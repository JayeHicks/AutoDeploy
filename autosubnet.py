""" Access a DynamoDB table to manage shared VPC's CIDR blocks
Jaye Hicks Consulting, 2017

Obligatory legal disclaimer:
 You are free to use this source code (this file and all other files 
 referenced in this file) "AS IS" WITHOUT WARRANTY OF ANY KIND, EITHER 
 EXPRESSED OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED 
 WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE 
 ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THIS SOURCE CODE IS WITH 
 YOU.  SHOULD THE SOURCE CODE PROVE DEFECTIVE, YOU ASSUME THE COST OF ALL 
 NECESSARY SERVICING, REPAIR OR CORRECTION. See the GNU GENERAL PUBLIC 
 LICENSE Version 3, 29 June 2007 for more details.

Overview:
Application stacks are automatically deployed into a shared VPC. This
module selects four available subnets from the VPC's CIDR range for
exclusive use by a deployed application stack.

CloudFormation details:
This module contains Python code designed to serve as a CloudFormation
custom resource.  When CloudFormation processing accesses the custom 
resource the module's autosubnet() function is called and two parameters
are passed in: 'event' and 'context'.  A response object is generated 
that contains either four CIDR blocks to be used by the deploying 
application stack or an informational message.

Prerequisites:
Proper execution of this Lambda function relies on the preexistence of 
a DynamoDB table that tracks the the assignment of CIDR blocks across
the VPC's entire IP range for all of the deployed application stacks.  
The table's name is passed in the 'event' argument.

Each deployed application is assgined 4 subnets.  This number of
subnets assigned is hard-wired in the CloudFormation templates that
invoke this custom resource.  The size of the subnets is hardwired in
this Python code and in the CloudFormation templates.

Args:
  event         details regarding the invocation of the Lambda function
  context       details regarding the execution of the Lambda function

Returns:
  A response object.  On success processing of a 'Create' request this 
  will be four CIDR blocks for the application to use.  Otherwise, the
  response object will contain information regarding the success / 
  failure of processing the incoming request. 
         
Dependencies:
  logging
  random
  string
  json
  ipaddress
  urllib.request
  boto3
  boto3.dynamodb.conditions.Key
  boto3.dynamodb.conditions.Attr
"""
#import logging

import random
import string
import json
import ipaddress
import urllib.request
import boto3
import botocore.exceptions
from   boto3.dynamodb.conditions import Key
from   boto3.dynamodb.conditions import Attr

#logging.basicConfig(filename='auto_subnet.log',level=logging.INFO)

subnet_size             = 24
num_sn_to_reserve       = 4


def _stack_has_cidrs(stack_id, table):
  """does a stack already have assigend cidrs"""
  has_cidrs = True
  
  try:
    results = table.query(IndexName='CidrsForStacks',
                          KeyConditionExpression=Key('StackId').eq(stack_id))             
  except Exception as e:
    #logging.error(f'DynamoDB table read failed: {e}')
    pass
    
  if(len(results['Items']) == 0):
    has_cidrs = False
    
  return(has_cidrs)


def _retrieve_stacks_cidrs(stack_id, table):
  """ retrieve the cidrs assigned to an application stack """
  assigned_cidrs = {}
  
  try:
    results = table.query(IndexName='CidrsForStacks',
                          KeyConditionExpression=Key('StackId').eq(stack_id))
    
    if(len(results['Items']) != num_sn_to_reserve):
      #logging.error('Too many or too few cidrs assigned to app stack.')
      pass
    else:
      assigned_cidrs = {
        'AppPublicCIDRA'  : results['Items'][0]['Cidr'],
        'AppPublicCIDRB'  : results['Items'][1]['Cidr'],
        'AppPrivateCIDRA' : results['Items'][2]['Cidr'],
        'AppPrivateCIDRB' : results['Items'][3]['Cidr']
      }
  except Exception as e:
    #logging.error(f'DynamoDB table read failed: {e}')
    pass
    
  return(assigned_cidrs)


def _assign_cidr_to_stack(cidr, stack_id, table):
  """ Update database tying cidr block to application stack """
  result = False
  try:
    table.put_item(Item={'Cidr' : cidr, 'StackId' : stack_id})
    result = True
  except Exception as e:
    #logging.error(f'DynamoDB table write failed: {e}')
    pass

  return(result)
  
  
def _cidr_in_use(cidr, table):
  """Detect cidr blocks already in use by an application stack """
  in_use = True
  
  try:
    result = table.query(KeyConditionExpression=Key('Cidr').eq(cidr))
    items = result['Items']
    if(len(items) == 0):
      in_use = False
  except Exception as e:
    #logging.error(f'DynamoDB table query failed: {e}')
    pass

  return(in_use)
  
      
def _allocate_cidrs_to_stack(vpc_cidr, stack_id, table): 
  """The VPC's overall CIDR range is provided as an input.  From this
  range an ascending ordered list of all possible subnets is created.
  A generator object is used to iterate over the list to find and 
  assign subnets to an application stack.  Subnets used by an app
  stack and then released are reused."""   
  result                  = False

  reserved_cidrs          = []
  cidrs_assigned_to_stack = 0
  
  try:
    network = ipaddress.ip_network(vpc_cidr)
    all_cidrs_in_network = network.subnets(new_prefix=subnet_size) #generator object
        
    for i in range(int(num_sn_to_reserve)):
      reserved_cidrs.append(str(next(all_cidrs_in_network)))
    reserved_cidrs = tuple(reserved_cidrs)
                           
    #"all_cidrs_in_network" has been advanced past the VPC reserved subnets
    #that are used shared infrastructure across all deployed app stacks.
    for cidr in all_cidrs_in_network:   
      if(cidrs_assigned_to_stack == num_sn_to_reserve): 
        break;
      if((str(cidr) not in reserved_cidrs) and 
         (not _cidr_in_use(str(cidr), table))):
        if(_assign_cidr_to_stack(str(cidr), stack_id, table)):
          cidrs_assigned_to_stack += 1
        else:
          #logging.error('Assigning a free cidr to stack failed')
          break;
        
    if(cidrs_assigned_to_stack == num_sn_to_reserve):
      result = True
    elif(cidrs_assigned_to_stack > 0):
      #logging.error('Failed to assign correct number of cidrs')
      if(not _free_cidrs(stack_id, table)):
        #logging.error('Failed to remove cidr assignments')
        pass        
  except Exception as e:
    #logging.error(f'Invalid network value(s) specified: {e}')
    pass
  return(result)
 
        
def _free_cidrs(stack_id, table):
  """ Delete all cidr blocks assigned to a stack. Note: will return
  True upon successful deletion or if stack had no assigned cidrs"""
  result = False
  try:
    result = table.scan(FilterExpression=Attr('StackId').eq(stack_id))
    for item in result['Items']:
      table.delete_item(Key={'Cidr' : item['Cidr']})
    result = True
  except Exception as e:
    #logging.error(f'Error occurred during database Item deletion: {e}')
    pass
  return(result)
        
        
def _generate_random_string(str_len=10):
  """Psuedo random string generator"""
  alpha_nums = string.ascii_letters + string.digits
  return (''.join((random.choice(alpha_nums) for _ in range(str_len))))
        
        
def _send_response(params, responsestatus, responsedata, reason):
  """Send a response object with results of processing request"""
  payload = {
    'StackId': params['StackId'],
    'Status' : responsestatus,
    'Reason' : reason,
    'RequestId': params['RequestId'],
    'LogicalResourceId': params['LogicalResourceId'],
    'PhysicalResourceId': params['LogicalResourceId'] + _generate_random_string(),
    'Data': responsedata}
    
  request = urllib.request.Request(url=params['ResponseURL'], 
                                   data=(json.dumps(payload)).encode(), 
                                   method='PUT')
  try:
    with urllib.request.urlopen(request) as post_data_back:
      pass
  except Exception as e:
    #logging.error(f'HTTP::PUT of data to CF custom resource response endpoint failed. {e}')
    pass
    
        
def _get_params(event):
  """ parameters passed by Lambda service via 'event' parameter """
  params = {}
  try:
    params['StackId']           = event['StackId']
    params['DynamoDBRegion']    = event['ResourceProperties']['DynamoDBRegion']
    params['DynamoDBTable']     = event['ResourceProperties']['DynamoDBTable']
    params['VPCCidr']           = event['ResourceProperties']['VPCCidr']
    params['RequestType']       = event['RequestType']
    params['LogicalResourceId'] = event['LogicalResourceId']
    params['ResponseURL']       = event['ResponseURL']
    params['RequestId']         = event['RequestId']
  except Exception as e:
    params = {}
    #logging.error(f'Failed to retrieve 1 or more parms from Lambda.event: {e}')
  
  return(params)


def _process_error(params, err_message):
  #logging.error(err_message)
  _send_response(params, 'FAILED', {'Error': err_message}, err_message)
 

def autosubnet(event, context):
  """ Attempt to allocate CIDR blocks from VPC's IP address range
  and assign them to an application stack.  (4) /24 cidrs assigned per
  stack"""
  params = _get_params(event)
  
  if(params):
    dynamo_access = boto3.resource('dynamodb', params['DynamoDBRegion'])

    table = dynamo_access.Table(params['DynamoDBTable'])
    try:
      table_exists = table.table_status.upper() in ('CREATING', 'UPDATING', 
                                                    'DELETING','ACTIVE')
    except Exception as e:
      #logging.error(f'DynamoDB table does not exist: {e}')
      table_exists = False
    
    if(table_exists):
      if(params['RequestType'].lower() == 'delete'):
        if(_free_cidrs(params['StackId'], table)):
          _send_response(params, 'SUCCESS', {}, 
            'Successfully freed cidrs from applicaiton stack')
        else:
          _process_error(params, 'Could not remove a stacks cidrs from database')
      elif(params['RequestType'].lower() == 'update'):
        _process_error(params, 'Update action not supported') 
      elif(params['RequestType'].lower() == 'create'): 
        if(not _stack_has_cidrs(params['StackId'], table)):
          if(_allocate_cidrs_to_stack(params['VPCCidr'], 
                                      params['StackId'],
                                      table)): 
            assigned_cidrs = _retrieve_stacks_cidrs(params['StackId'], table)
            if(assigned_cidrs):
              _send_response(params, 'SUCCESS', assigned_cidrs, 
                'Successfully assigned cidrs to application stack.' )
            else:
             _process_error(params, 'Failed to assign cidrs to application stack')
          else:
            _process_error(params, 'Application stack assigned too few or too many cidrs')
        else:
          _process_error(params, 'Application stack already has assgiend cidrs')
      else:
        err_message = 'Action: ' + params['RequestType'] + ' is not supported'
        _process_error(params, err_message) 
    else:
      _process_error(params, 'DynamoDB table for cidrs does not exist') 
  else:
    _process_error(params, 'Invalid parameters')