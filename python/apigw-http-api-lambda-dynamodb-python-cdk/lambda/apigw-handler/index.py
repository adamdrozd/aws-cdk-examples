# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import os
import json
import logging
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb_client = boto3.client("dynamodb")


def handler(event, context):
    # Extract security context
    request_id = context.request_id
    source_ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'unknown')
    http_method = event.get('httpMethod', 'unknown')
    path = event.get('path', 'unknown')
    
    # Log request received with security context
    logger.info(json.dumps({
        'event': 'request_received',
        'request_id': request_id,
        'source_ip': source_ip,
        'http_method': http_method,
        'path': path,
    }))
    
    table = os.environ.get("TABLE_NAME")
    logger.info(json.dumps({
        'event': 'table_loaded',
        'request_id': request_id,
        'table_name': table
    }))
    
    try:
        if event.get("body"):
            item = json.loads(event["body"])
            logger.info(json.dumps({
                'event': 'processing_request',
                'request_id': request_id,
                'item_id': item.get('id'),
                'has_payload': True
            }))
            
            year = str(item["year"])
            title = str(item["title"])
            id = str(item["id"])
            
            dynamodb_client.put_item(
                TableName=table,
                Item={"year": {"N": year}, "title": {"S": title}, "id": {"S": id}},
            )
            
            logger.info(json.dumps({
                'event': 'item_inserted',
                'request_id': request_id,
                'item_id': id
            }))
            
            message = "Successfully inserted data!"
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": message}),
            }
        else:
            logger.info(json.dumps({
                'event': 'processing_request',
                'request_id': request_id,
                'has_payload': False
            }))
            
            default_id = str(uuid.uuid4())
            dynamodb_client.put_item(
                TableName=table,
                Item={
                    "year": {"N": "2012"},
                    "title": {"S": "The Amazing Spider-Man 2"},
                    "id": {"S": default_id},
                },
            )
            
            logger.info(json.dumps({
                'event': 'item_inserted',
                'request_id': request_id,
                'item_id': default_id
            }))
            
            message = "Successfully inserted data!"
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": message}),
            }
    except Exception as e:
        logger.error(json.dumps({
            'event': 'error',
            'request_id': request_id,
            'error_type': type(e).__name__,
            'error_message': str(e)
        }))
        raise
