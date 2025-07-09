#!/usr/bin/env python3
"""
End‑to‑end AWS bootstrap with boto3.client only:
* Create a VPC, public subnet, Internet Gateway, and route table
* Create a security group opening SSH (TCP 22) and TCP 5000 to 0.0.0.0/0
* Launch one Amazon Linux 2023 EC2 instance
* Install Docker and Docker Compose using user data
* Print all resources and connection info
"""

import os
import boto3
from botocore.exceptions import ClientError


REGION = "us-east-1"
VPC_CIDR = "10.0.0.0/16"
SUBNET_CIDR = "10.0.1.0/24"
AVAILABILITY_ZONE = f"{REGION}a"
KEY_NAME = "ec2-keypair"  
KEY_FILE = f"{KEY_NAME}.pem"
INSTANCE_TYPE = "t2.micro"
PROJECT_NAME = "demo-client"


ec2 = boto3.client("ec2", region_name=REGION)

def latest_ubuntu_ami() -> str:
    """
    Return the latest Ubuntu 22.04 LTS (Jammy) AMI for x86_64 architecture.
    Canonical's AWS Account ID: 099720109477
    """
    response = ec2.describe_images(
        Owners=["099720109477"],
        Filters=[
            {
                "Name": "name",
                "Values": ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
            },
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "virtualization-type", "Values": ["hvm"]},
            {"Name": "root-device-type", "Values": ["ebs"]},
        ]
    )

    images = response["Images"]

    if not images:
        raise ValueError("No Ubuntu AMIs found. Check region and filters.")

    return max(images, key=lambda i: i["CreationDate"])["ImageId"]


def create_key_pair():
    try:
        print(f"Creating new key pair: {KEY_NAME}")
        key_pair = ec2.create_key_pair(KeyName=KEY_NAME)
        private_key = key_pair["KeyMaterial"]

        with open(KEY_FILE, "w") as f:
            f.write(private_key)
        os.chmod(KEY_FILE, 0o400)
        print(f"Key saved to {KEY_FILE}")
    except ClientError as e:
        if "InvalidKeyPair.Duplicate" in str(e):
            print(f"ℹKey pair {KEY_NAME} already exists. Skipping creation.")
        else:
            raise

def main():
    try:
        create_key_pair()

        # VPC
        vpc_id = ec2.create_vpc(
            CidrBlock=VPC_CIDR,
            TagSpecifications=[{
                "ResourceType": "vpc",
                "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-vpc"}]
            }]
        )["Vpc"]["VpcId"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        # Subnet
        subnet_id = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=SUBNET_CIDR,
            AvailabilityZone=AVAILABILITY_ZONE,
            TagSpecifications=[{
                "ResourceType": "subnet",
                "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-subnet"}]
            }]
        )["Subnet"]["SubnetId"]
        ec2.modify_subnet_attribute(
            SubnetId=subnet_id,
            MapPublicIpOnLaunch={"Value": True}
        )

        # Internet Gateway
        igw_id = ec2.create_internet_gateway(
            TagSpecifications=[{
                "ResourceType": "internet-gateway",
                "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-igw"}]
            }]
        )["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        # Route Table
        rtb_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
        ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet_id)

        # Security Group
        sg_id = ec2.create_security_group(
            VpcId=vpc_id,
            GroupName=f"{PROJECT_NAME}-sg",
            Description="Allow SSH and TCP 5000",
            TagSpecifications=[{
                "ResourceType": "security-group",
                "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-sg"}]
            }]
        )["GroupId"]

        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 5000,
                    "ToPort": 5000,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "App Port"}],
                }
            ]
        )

        # AMI
        ami_id = latest_ubuntu_ami()

        # User Data Script
        user_data = """#!/bin/bash
set -eux
apt-get update -y
apt-get install -y docker.io
systemctl enable --now docker
usermod -aG docker ubuntu     # default Ubuntu login user
curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
"""


        # Launch EC2
        run_resp = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=INSTANCE_TYPE,
            KeyName=KEY_NAME,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            UserData=user_data,
            MinCount=1, MaxCount=1,
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": f"{PROJECT_NAME}-ec2"}]
            }]
        )
        instance_id = run_resp["Instances"][0]["InstanceId"]
        ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

        public_ip = ec2.describe_instances(InstanceIds=[instance_id]) \
                       ["Reservations"][0]["Instances"][0] \
                       .get("PublicIpAddress")

        print("\n All resources created:")
        print(f"VPC:            {vpc_id}")
        print(f"Subnet:         {subnet_id}")
        print(f"Internet GW:    {igw_id}")
        print(f"Route Table:    {rtb_id}")
        print(f"Security Group: {sg_id}")
        print(f"EC2 Instance:   {instance_id}")
        print(f"Public IP:      {public_ip}")
        print(f"\nSSH command:")
        print(f"ssh -i {KEY_FILE} ubuntu@{public_ip}")

    except ClientError as err:
        print("AWS error:", err)
    except Exception as exc:
        print("Unexpected error:", exc)

if __name__ == "__main__":
    main()
