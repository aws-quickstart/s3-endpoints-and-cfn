BUCKETNAME=tonynv
aws s3 cp --recursive sigv2-to-sigv4 s3://${BUCKETNAME}/sigv2-to-sigv4/
aws s3 cp --recursive sigv2-to-sigv4 s3://${BUCKETNAME}-us-east-1/sigv2-to-sigv4/
aws s3 cp --recursive sigv2-to-sigv4 s3://${BUCKETNAME}-us-west-2/sigv2-to-sigv4/

