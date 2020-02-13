#!/bin/bash 
# This script requires awscli to be install and added to you path
# Script require s3 put-bucket-policy permission 
# tonynv@amazon.com

cmd_exists()
{
  command -v "$1" >/dev/null 2>&1
}


if [ $# -eq 0 ]
  then
    echo -e "\nPlease provide s3 bucket name '$0 <s3_bucket>' to run this command!\n"
    exit 1;
  else
     # Set bucket name
     S3_BUCKET=$1

     # Check for aws cli
     if cmd_exists aws; then
     AWS_CLI=true
     else
       echo 'Please install awscli (aws command not found in path)'
       exit 1
     fi
fi

if $(aws s3api head-bucket --bucket ${S3_BUCKET}); then
  sed "s,REPLACE,${S3_BUCKET},g" deny_sigv2_requests.jsonstub > deny_sigv2_requests-${S3_BUCKET}.json
  aws s3api put-bucket-policy --bucket $S3_BUCKET --policy file://deny_sigv2_requests-${S3_BUCKET}.json
  aws s3api get-bucket-policy --bucket $S3_BUCKET 
else
  echo "[$S3_BUCKET] does not exist or you have no permmision to it"
  exit 1
fi
