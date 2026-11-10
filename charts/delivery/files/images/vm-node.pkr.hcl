# The golden image of aws-asg Targets (agent-platform docs/DELIVERY_V3.md D4): Amazon Linux 2023, Docker, the
# CloudFormation helper (cfn-signal) and /usr/local/bin/runway-app. Apps run as containers on it; no Release is baked
# into the image. The image-bake Workflow runs `packer build` with the variables below, in the Target account.
packer {
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = "= 1.8.4"
    }
  }
}

variable "name" {
  type        = string
  description = "AMI name: agent-runway-<image name>-<version>."
}

variable "region" {
  type = string
}

variable "architecture" {
  type = string
  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "The architecture must be arm64 or x86_64."
  }
}

variable "instance_type" {
  type = string
}

variable "subnet_id" {
  type        = string
  description = "A private subnet with a route to the internet (NAT) for package downloads."
}

variable "image_id" {
  type        = string
  description = "The Agent Runway golden image ID, recorded as a tag."
}

source "amazon-ebs" "node" {
  region        = var.region
  ami_name      = var.name
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  ssh_interface = "session_manager"
  communicator  = "ssh"
  ssh_username  = "ec2-user"
  # The builder needs an instance profile with AmazonSSMManagedInstanceCore for Session Manager.
  temporary_iam_instance_profile_policy_document {
    Version = "2012-10-17"
    Statement {
      Effect   = "Allow"
      Action   = ["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]
      Resource = ["*"]
    }
  }
  imds_support = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  source_ami_filter {
    owners      = ["amazon"]
    most_recent = true
    filters = {
      name                = "al2023-ami-2023.*-kernel-*-${var.architecture}"
      architecture        = var.architecture
      virtualization-type = "hvm"
      root-device-type    = "ebs"
    }
  }
  encrypt_boot = true
  tags = {
    "opspresso.com/runway-image" = var.image_id
  }
  run_tags = {
    "opspresso.com/runway-image" = var.image_id
  }
}

build {
  sources = ["source.amazon-ebs.node"]

  provisioner "file" {
    source      = "runway-app.py"
    destination = "/tmp/runway-app"
  }

  provisioner "shell" {
    inline = [
      "set -euxo pipefail",
      "sudo dnf -y update",
      "sudo dnf -y install docker aws-cfn-bootstrap",
      "sudo systemctl enable docker",
      "sudo install -m 0755 /tmp/runway-app /usr/local/bin/runway-app",
      "sudo install -d -m 0700 /etc/runway",
      "sudo dnf clean all",
      "sudo rm -rf /var/cache/dnf /tmp/runway-app",
    ]
  }

  # The image-bake Workflow reads the AMI ID from here.
  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
