#!/usr/bin/env python3
"""
Create from scratch, entirely with boto3.client calls:

1. VPC, IGW, public route‑table
2. Two public subnets (AZ‑a, AZ‑b) that auto‑assign public IPs
3. Security group for EC2 (SSH + app port 5000)
4. Security group for ALB (HTTP 80)
5. One Ubuntu‑22.04 EC2 in each AZ, running a sample container on :5000
6. Application Load Balancer (internet‑facing) on :80 → target group :5000
"""

import os, time, boto3
from botocore.exceptions import ClientError


REGION        = "us-east-1"
KEY_NAME      = "ec2-keypair"
KEY_FILE      = f"{KEY_NAME}.pem"
PROJECT       = "lb-demo"
APP_PORT      = 5000
INSTANCE_TYPE = "t2.micro"

VPC_CIDR      = "10.0.0.0/16"
SUBNET_A_CIDR = "10.0.1.0/24"
SUBNET_B_CIDR = "10.0.2.0/24"
AZ_A          = f"{REGION}a"
AZ_B          = f"{REGION}b"


ec2   = boto3.client("ec2",   region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)

# ---------- helpers ----------------------------------------------------------
def latest_ubuntu_ami() -> str:
    """Return latest Ubuntu 22.04 LTS AMI ID in the region."""
    imgs = ec2.describe_images(
        Owners=["099720109477"],
        Filters=[{"Name":"name",
                  "Values":["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]},
                 {"Name":"architecture","Values":["x86_64"]},
                 {"Name":"virtualization-type","Values":["hvm"]},
                 {"Name":"root-device-type","Values":["ebs"]}]
    )["Images"]
    if not imgs:
        raise RuntimeError("No Ubuntu AMIs found")
    return max(imgs, key=lambda i: i["CreationDate"])["ImageId"]

def ensure_key_pair():
    try:
        kp = ec2.create_key_pair(KeyName=KEY_NAME)
        with open(KEY_FILE, "w") as f:
            f.write(kp["KeyMaterial"])
        os.chmod(KEY_FILE, 0o400)
        print(f"✔  New key saved to {KEY_FILE}")
    except ClientError as e:
        if "InvalidKeyPair.Duplicate" in str(e):
            print(f"ℹ  Key pair {KEY_NAME} already exists – using it.")
        else:
            raise


def main():
    ensure_key_pair()
    ami_id = latest_ubuntu_ami()

   
    vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR)["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value":True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value":True})

    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    rtb_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0",
                     GatewayId=igw_id)

    
    subnet_a = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=SUBNET_A_CIDR, AvailabilityZone=AZ_A
    )["Subnet"]["SubnetId"]
    subnet_b = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock=SUBNET_B_CIDR, AvailabilityZone=AZ_B
    )["Subnet"]["SubnetId"]

    for subnet in (subnet_a, subnet_b):
        ec2.modify_subnet_attribute(SubnetId=subnet,
                                    MapPublicIpOnLaunch={"Value":True})
        ec2.associate_route_table(RouteTableId=rtb_id, SubnetId=subnet)

   
    sg_ec2 = ec2.create_security_group(
        VpcId=vpc_id, GroupName=f"{PROJECT}-inst-sg",
        Description="SSH + app 5000")["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg_ec2,
        IpPermissions=[
            {"IpProtocol":"tcp","FromPort":22,"ToPort":22,
             "IpRanges":[{"CidrIp":"0.0.0.0/0","Description":"SSH"}]},
            {"IpProtocol":"tcp","FromPort":APP_PORT,"ToPort":APP_PORT,
             "IpRanges":[{"CidrIp":"0.0.0.0/0","Description":"App"}]},
        ])

    sg_alb = ec2.create_security_group(
        VpcId=vpc_id, GroupName=f"{PROJECT}-alb-sg",
        Description="ALB 80")["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg_alb,
        IpPermissions=[{
            "IpProtocol":"tcp","FromPort":80,"ToPort":80,
            "IpRanges":[{"CidrIp":"0.0.0.0/0","Description":"HTTP"}]}])

   
    user_data = f"""#!/bin/bash
set -eux
apt-get update -y
apt-get install -y docker.io
systemctl enable --now docker
usermod -aG docker ubuntu
docker run -d -p {APP_PORT}:{APP_PORT} --name hello \
  nginxdemos/hello
"""
    inst_ids = []
    for subnet in (subnet_a, subnet_b):
        resp = ec2.run_instances(
            ImageId=ami_id, InstanceType=INSTANCE_TYPE, KeyName=KEY_NAME,
            SubnetId=subnet, SecurityGroupIds=[sg_ec2],
            UserData=user_data, MinCount=1, MaxCount=1,
            TagSpecifications=[{
                "ResourceType":"instance",
                "Tags":[{"Key":"Name","Value":f"{PROJECT}-ec2"}]
            }]
        )
        inst_ids.append(resp["Instances"][0]["InstanceId"])

    ec2.get_waiter("instance_running").wait(InstanceIds=inst_ids)
    print("EC2 instances:", *inst_ids)

    tg_name = f"{PROJECT}-tg-{int(time.time())}"
    tg_arn = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP", Port=APP_PORT, VpcId=vpc_id,
        HealthCheckProtocol="HTTP",
        HealthCheckPort=str(APP_PORT),
        HealthCheckPath="/",
        TargetType="instance"
    )["TargetGroups"][0]["TargetGroupArn"]

    elbv2.register_targets(
        TargetGroupArn=tg_arn,
        Targets=[{"Id":iid,"Port":APP_PORT} for iid in inst_ids])

    alb = elbv2.create_load_balancer(
        Name=f"{PROJECT}-alb-{int(time.time())}",
        Subnets=[subnet_a, subnet_b],
        SecurityGroups=[sg_alb],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4"
    )["LoadBalancers"][0]

    elbv2.create_listener(
        LoadBalancerArn=alb["LoadBalancerArn"],
        Protocol="HTTP", Port=80,
        DefaultActions=[{"Type":"forward","TargetGroupArn":tg_arn}]
    )

 
    dns = alb["DNSName"]
    print("\nStack ready")
    print("VPC:             ", vpc_id)
    print("Subnets:         ", subnet_a, subnet_b)
    print("Instances:       ", *inst_ids)
    print("ALB DNS:         ", dns)
    print(f"\nOpen http://{dns}/ in your browser\n")


if __name__ == "__main__":
    main()

