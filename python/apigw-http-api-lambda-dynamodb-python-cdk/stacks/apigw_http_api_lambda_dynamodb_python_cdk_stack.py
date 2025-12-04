# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb_,
    aws_lambda as lambda_,
    aws_apigateway as apigw_,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
    aws_cloudtrail as cloudtrail,
    aws_s3 as s3,
    aws_cloudwatch as cloudwatch,
    aws_synthetics as synthetics,
    Duration,
    RemovalPolicy,
)
from constructs import Construct

TABLE_NAME = "demo_table"


class ApigwHttpApiLambdaDynamodbPythonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC
        vpc = ec2.Vpc(
            self,
            "Ingress",
            cidr="10.1.0.0/16",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private-Subnet", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24
                )
            ],
        )
        
        # Enable VPC Flow Logs
        vpc_flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.DESTROY,
        )
        
        vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(vpc_flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )
        
        # Create VPC endpoint for DynamoDB
        dynamo_db_endpoint = ec2.GatewayVpcEndpoint(
            self,
            "DynamoDBVpce",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            vpc=vpc,
        )

        # This allows to customize the endpoint policy
        dynamo_db_endpoint.add_to_policy(
            iam.PolicyStatement(  # Restrict to listing and describing tables
                principals=[iam.AnyPrincipal()],
                actions=[                "dynamodb:DescribeStream",
                "dynamodb:DescribeTable",
                "dynamodb:Get*",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:CreateTable",
                "dynamodb:Delete*",
                "dynamodb:Update*",
                "dynamodb:PutItem"],
                resources=["*"],
            )
        )

        # Create VPC endpoint for X-Ray (required for Lambda in isolated subnets)
        ec2.InterfaceVpcEndpoint(
            self,
            "XRayVpce",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointAwsService.XRAY,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
        )

        # Create DynamoDb Table with Point-in-Time Recovery
        demo_table = dynamodb_.Table(
            self,
            TABLE_NAME,
            partition_key=dynamodb_.Attribute(
                name="id", type=dynamodb_.AttributeType.STRING
            ),
            point_in_time_recovery=True,
        )

        # Create the Lambda function to receive the request with X-Ray tracing
        api_hanlder = lambda_.Function(
            self,
            "ApiHandler",
            function_name="apigw_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("lambda/apigw-handler"),
            handler="index.handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            memory_size=1024,
            timeout=Duration.minutes(5),
            log_retention=logs.RetentionDays.ONE_YEAR,
            tracing=lambda_.Tracing.ACTIVE,
        )

        # grant permission to lambda to write to demo table
        demo_table.grant_write_data(api_hanlder)
        api_hanlder.add_environment("TABLE_NAME", demo_table.table_name)

        # Create log group for API Gateway access logs
        api_log_group = logs.LogGroup(
            self,
            "ApiGatewayAccessLogs",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create API Gateway with access logging and X-Ray tracing
        api = apigw_.LambdaRestApi(
            self,
            "Endpoint",
            handler=api_hanlder,
            deploy_options=apigw_.StageOptions(
                access_log_destination=apigw_.LogGroupLogDestination(api_log_group),
                access_log_format=apigw_.AccessLogFormat.json_with_standard_fields(
                    caller=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                tracing_enabled=True,
            ),
        )

        # CloudWatch Alarm for Lambda errors
        cloudwatch.Alarm(
            self,
            "LambdaErrorAlarm",
            metric=api_hanlder.metric_errors(),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Alert when Lambda function errors occur",
        )

        # CloudWatch Alarm for API Gateway 5xx errors
        cloudwatch.Alarm(
            self,
            "ApiGateway5xxAlarm",
            metric=api.metric_server_error(),
            threshold=5,
            evaluation_periods=2,
            alarm_description="Alert when API Gateway 5xx errors occur",
        )

        # CloudWatch Alarm for API Gateway 4xx errors
        cloudwatch.Alarm(
            self,
            "ApiGateway4xxAlarm",
            metric=api.metric_client_error(),
            threshold=10,
            evaluation_periods=2,
            alarm_description="Alert when API Gateway 4xx errors exceed threshold",
        )

        # S3 bucket for Canary artifacts
        canary_bucket = s3.Bucket(
            self,
            "CanaryArtifactsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # CloudWatch Synthetic Canary to monitor API endpoint
        canary = synthetics.Canary(
            self,
            "ApiCanary",
            runtime=synthetics.Runtime.SYNTHETICS_PYTHON_SELENIUM_3_0,
            test=synthetics.Test.custom(
                code=synthetics.Code.from_inline(f"""
import json
from aws_synthetics.selenium import synthetics_webdriver as webdriver
from aws_synthetics.common import synthetics_logger as logger
from aws_synthetics.common import synthetics_configuration

def handler(event, context):
    logger.info("Starting canary test")
    url = "{api.url}"
    
    # Configure synthetics
    synthetics_configuration.set_config({{
        "screenshot_on_step_start": False,
        "screenshot_on_step_success": False,
        "screenshot_on_step_failure": True
    }})
    
    browser = webdriver.Chrome()
    browser.set_page_load_timeout(30)
    
    try:
        browser.get(url)
        logger.info(f"Successfully loaded {{url}}")
        logger.info(f"Response status: {{browser.title}}")
    finally:
        browser.quit()
    
    logger.info("Canary test completed successfully")
    return {{"statusCode": 200}}
                """),
                handler="handler",
            ),
            artifacts_bucket_location=synthetics.ArtifactsBucketLocation(
                bucket=canary_bucket
            ),
            schedule=synthetics.Schedule.rate(Duration.minutes(5)),
            enable_auto_start=True,
        )

        # CloudWatch Alarm for Canary failures
        cloudwatch.Alarm(
            self,
            "CanaryFailureAlarm",
            metric=canary.metric_failed(),
            threshold=1,
            evaluation_periods=1,
            alarm_description="Alert when synthetic canary fails",
        )

        # Create S3 bucket for CloudTrail logs
        trail_bucket = s3.Bucket(
            self,
            "CloudTrailBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Create CloudTrail
        cloudtrail.Trail(
            self,
            "CloudTrail",
            bucket=trail_bucket,
            is_multi_region_trail=True,
            include_global_service_events=True,
        )
