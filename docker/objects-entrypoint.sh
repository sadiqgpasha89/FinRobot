#!/bin/sh
# Starts SeaweedFS with its S3 gateway protected by the access key from the environment (the image's default is no auth at all).
set -eu
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}" "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
cat > /tmp/s3.json <<JSON
{"identities":[{"name":"finrobot","credentials":[{"accessKey":"${S3_ACCESS_KEY}","secretKey":"${S3_SECRET_KEY}"}],"actions":["Admin","Read","Write","List","Tagging"]}]}
JSON
exec weed server -s3 -s3.config=/tmp/s3.json -dir=/data -master.volumeSizeLimitMB=256 -ip.bind=0.0.0.0
