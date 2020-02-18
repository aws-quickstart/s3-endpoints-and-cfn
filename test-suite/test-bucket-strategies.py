from cfn_tools import load_yaml, CfnYamlDumper
from pathlib import Path
import tempfile
import shutil
from collections import OrderedDict
from functools import partial
import boto3
from random import choice
import string
from multiprocessing.dummy import Pool as ThreadPool
import sys
from time import sleep
import yaml


KEY_NAME = "tester" # Must have a key with this name in each region testing
VPC_TEMPLATES = ["aws-cli.template.yaml", "cfn-init.template.yaml"]
PATH_PREFIX = Path("./").resolve()
TEST_CASES_PATH = PATH_PREFIX / "test-cases"
TEMPLATES_PATH = TEST_CASES_PATH / 'templates'
BUCKET_NAME_PREFIX = "qs-sigv4-test-"
BUCKET_NAME_RANDOM = "".join([choice(string.ascii_lowercase) for _ in range(8)])

MASTER_TEMPLATES = [
    "aws-cli/aws-cli.template.yaml",
    "cfn-init/cfn-init.template.yaml",
    "nested/master.template.yaml"
]

REGION_MAP = load_yaml(Path("./regions.yaml").read_text())
SV4_POLICY = """{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Test",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": "arn:aws:s3:::%s/*",
            "Condition": {"StringEquals": {"s3:signatureversion": "AWS"}}
        }
    ]
}"""

CLIENTS = {}


def dump_yaml(source):
    CfnYamlDumper.ignore_aliases = lambda self, data: True
    return yaml.dump(source, Dumper=CfnYamlDumper, default_flow_style=False, allow_unicode=True)


def get_test_cases():
    test_cases = {}
    for file in TEST_CASES_PATH.glob('*.yaml'):
        with file.open("r") as fp:
            test_case = load_yaml(fp)
        test_cases[file.stem] = test_case
    return test_cases


def build_tests(test_cases):
    base_path = Path(tempfile.mkdtemp())
    for test_name, test in test_cases.items():
        test_path = base_path / test_name
        test_path.mkdir()
        for i in TEMPLATES_PATH.rglob('*'):
            dst = test_path / i.relative_to(TEMPLATES_PATH)
            if i.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            if i.is_file():
                if str(i).endswith("template.yaml"):
                    render_template(i, dst, test)
                else:
                    shutil.copy(str(i), str(dst))
    return base_path


def render_template(src, dst, test):
    with src.open('r') as fp:
        yaml = load_yaml(fp)
    # make sure there are the needed sections in the yaml
    for s in ['Parameters', 'Conditions']:
        if not yaml.get(s):
            yaml[s] = {}
    yaml['Parameters'].update(test.get('Parameters', {}))
    yaml['Conditions'].update(test.get('Conditions', {}))
    for key, value in test['Replacements'].items():
        deep_replace(key, value, yaml)
    with dst.open('w') as fp:
        fp.write(dump_yaml(yaml))


def deep_replace(key, value, yaml):
    if isinstance(yaml, (dict, OrderedDict)):
        if key in yaml:
            yaml[key] = value
        for v in yaml.values():
            deep_replace(key, value, v)
    if isinstance(yaml, list):
        for i in yaml:
            deep_replace(key, value, i)


def create_buckets(temp_path):
    cleanup_buckets()

    for region_map in REGION_MAP.values():
        pool = ThreadPool(len(region_map["regions"]))
        args = [(region_map["profile"], r, temp_path) for r in region_map["regions"]]
        pool.map(create_bucket, args)
        pool.close()
        pool.join()


def create_bucket(args):
    profile, region, temp_path = args
    kwargs = {}
    s3 = get_client(profile, "s3", region)
    kwargs["Bucket"] = build_bucket_name(region)
    files = [(str(file), str(file.relative_to(temp_path))) for file in temp_path.rglob('*') if file.is_file()]
    pool = ThreadPool(8)
    func = partial(create_object, s3=s3, bucket=kwargs["Bucket"])
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {'LocationConstraint': region}
    s3.create_bucket(**kwargs)
    s3.get_waiter('bucket_exists').wait(Bucket=kwargs["Bucket"])
    s3.put_bucket_policy(Bucket=kwargs["Bucket"], Policy=SV4_POLICY % kwargs["Bucket"])
    pool.map(func, files)
    kwargs["Bucket"] = build_bucket_name(region, region_suffix=True)
    func = partial(create_object, s3=s3, bucket=kwargs["Bucket"])
    s3.create_bucket(**kwargs)
    s3.get_waiter('bucket_exists').wait(Bucket=kwargs["Bucket"])
    s3.put_bucket_policy(Bucket=kwargs["Bucket"], Policy=SV4_POLICY % kwargs["Bucket"])
    pool.map(func, files)
    pool.close()
    pool.join()


def create_object(args, s3, bucket):
    src, dst = args
    while True:
        try:
            s3.upload_file(src, bucket, dst)
            break
        except Exception as e:
            if "The specified bucket does not exist" not in str(e):
                raise
            sleep(5)


def build_bucket_name(region, region_suffix=False):
    if region_suffix:
        return BUCKET_NAME_PREFIX + BUCKET_NAME_RANDOM + f"-{region}"
    return BUCKET_NAME_PREFIX + f"{region}-" + BUCKET_NAME_RANDOM


def cleanup_buckets():
    for region_map in REGION_MAP.values():
        if region_map["regions"]:
            s3 = get_client(region_map["profile"], "s3", region_map["regions"][0])
            pool = ThreadPool(len(region_map["regions"]))
            func = partial(delete_bucket, s3=s3)
            pool.map(func, s3.list_buckets()["Buckets"])
            pool.close()
            pool.join()


def delete_bucket(b, s3):
    if b["Name"].startswith(BUCKET_NAME_PREFIX):
        try:
            response = s3.list_object_versions(Bucket=b["Name"])
        except s3.exceptions.NoSuchBucket:
            return
        objects = response.get('Versions', []) + response.get('DeleteMarkers', [])
        delete = []
        for item in objects:
            delete.append({"Key": item['Key'], "VersionId": item["VersionId"]})
        if objects:
            s3.delete_objects(Bucket=b["Name"], Delete={"Objects": delete})
        s3.delete_bucket(Bucket=b["Name"])


def execute_tests(test_cases):
    strategies = ["regionsuffix", "anyregion", "oneregion"]
    pool = ThreadPool(len(strategies))
    func = partial(execute_test_for_strategy, test_cases=test_cases)
    pool.map(func, strategies)
    pool.close()
    pool.join()


def execute_test_for_strategy(strategy, test_cases):
    if strategy == "regionsuffix":
        entrypoint_bucket = build_bucket_name("us-east-1")
        params = build_params(bucket=BUCKET_NAME_PREFIX + BUCKET_NAME_RANDOM)
    # TODO: other strategies
    else:
        return
    pool = ThreadPool(len(test_cases))
    func = partial(execute_test, entrypoint_bucket=entrypoint_bucket, params=params)
    pool.map(func, test_cases.keys())
    pool.close()
    pool.join()


def execute_test(test_name, entrypoint_bucket, params):
    params = params.copy()
    template_url_prefix = f"https://{entrypoint_bucket}.s3.amazonaws.com/{test_name}/"
    stacks = []
    for t in MASTER_TEMPLATES:
        for region_map in REGION_MAP.values():
            for region in region_map["regions"]:
                params.append({"ParameterKey": "KeyPrefix", "ParameterValue": f"{str(test_name)}/{t.split('/')[0]}/"})
                stacks.append((template_url_prefix + t, region_map["profile"], region, params))
    pool = ThreadPool(len(stacks))
    stack_ids = pool.map(create_stack, stacks)
    pool.close()
    pool.join()
    results = {}
    for s in stack_ids:
        success, stack_id, template_url, profile = s
        strategy = template_url.split('/')[-3]
        template = template_url.split('/')[-2]
        region = stack_id.split(":")[3]
        results[template] = results.get(template, {})
        results[template][profile] = results[template].get(profile, {})
        if success is not True:
            results[template][profile][region] = success
        else:
            results[template][profile][region] = "SUCCESS"
    print("===================================================")
    print(f"======================{strategy}======================")
    print("===================================================")
    print(dump_yaml(results))


def get_default_vpc(profile, region):
    ec2 = get_client(profile, "ec2", region)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])['Vpcs']
    if not vpcs:
        raise Exception(f"Cannot test profile {profile} in {region} because there is no default VPC")
    return vpcs[0]['VpcId']


def get_default_subnet(profile, region, vpc):
    ec2 = get_client(profile, "ec2", region)
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    if not subnets:
        raise Exception(f"Cannot test profile {profile} in {region} because {vpc} has no subnets in it")
    return subnets[0]["SubnetId"]


def create_stack(args):
    template_url, profile, region, params = args
    params = {p["ParameterKey"]: p["ParameterValue"] for p in params}
    # add vpc params
    if [t for t in VPC_TEMPLATES if template_url.endswith(t)]:
        params["KeyPairName"] = KEY_NAME
        params["VPCID"] = get_default_vpc(profile, region)
        params["SubnetId"] = get_default_subnet(profile, region, params["VPCID"])
    strategy = template_url.split('/')[-3]
    template = template_url.split('/')[-2]
    params["KeyPrefix"] = f"{strategy}/{template}/"
    params = build_params(params["BucketName"], **params)
    cfn = get_client(profile, "cloudformation", region)
    kwargs = {
        "TemplateURL": template_url,
        "Parameters": params,
        "StackName": BUCKET_NAME_PREFIX + "".join([choice(string.ascii_lowercase) for _ in range(8)]),
        "DisableRollback": True,
        "Capabilities": ['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
    }
    retries = 0
    while True:
        try:
            stack_id = cfn.create_stack(**kwargs)['StackId']
            break
        except Exception as e:
            retries += 1
            if retries > 10:
                return str(e), "None", template_url, profile
            sleep(5)
    while True:
        status = cfn.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
        if status == "CREATE_COMPLETE":
            return True, stack_id, template_url, profile
        elif status in ['CREATE_FAILED', 'ROLLBACK_IN_PROGRESS', 'ROLLBACK_FAILED', 'ROLLBACK_COMPLETE', 'DELETE_IN_PROGRESS', 'DELETE_FAILED', 'DELETE_COMPLETE']:
            errors = ""
            events = cfn.describe_stack_events(StackName=stack_id).get('StackEvents', [])
            for e in events:
                if e['ResourceStatus'] == 'CREATE_FAILED' and not e['ResourceStatusReason'].startswith("The following resource(s) failed to create:"):
                    errors += f"{e['LogicalResourceId']}: {e['ResourceStatusReason']}\n"
            return errors, stack_id, template_url, profile
        else:
            sleep(30+choice(range(60)))


def cleanup_stacks():
    regions = []
    for region_map in REGION_MAP.values():
        for region in region_map["regions"]:
            regions.append((region_map["profile"], region))
    pool = ThreadPool(len(regions))
    pool.map(cleanup_stacks_in_region, regions)
    pool.close()
    pool.join()


def cleanup_stacks_in_region(args):
    profile, region = args
    cfn = get_client(profile, "cloudformation", region)
    stack_summaries = cfn.list_stacks(
        StackStatusFilter=[
            'CREATE_FAILED', 'CREATE_COMPLETE', 'ROLLBACK_FAILED', 'ROLLBACK_COMPLETE', 'DELETE_FAILED', 'UPDATE_COMPLETE','UPDATE_ROLLBACK_FAILED', 'UPDATE_ROLLBACK_COMPLETE'
        ]
    )['StackSummaries']
    for s in stack_summaries:
        if s["StackName"].startswith(BUCKET_NAME_PREFIX):
            cfn.delete_stack(StackName=s["StackName"])


def get_client(profile, service, region):
    if CLIENTS.get(profile+service+region):
        return CLIENTS.get(profile+service+region)
    client = boto3.Session(profile_name=profile).client(service, region_name=region)
    CLIENTS[profile + service + region] = client
    return client


def build_params(bucket, **kwargs):
    params = [
        {"ParameterKey": "BucketName", "ParameterValue": str(bucket)}
    ]
    for k, v in kwargs.items():
        params.append({"ParameterKey": str(k), "ParameterValue": str(v)})
    return params


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == "cleanup":
            cleanup_buckets()
            cleanup_stacks()
            sys.exit(0)
    print("getting test cases...")
    tests = get_test_cases()
    print("building tests from test-cases...")
    temp_path = build_tests(tests)
    print("cleaning up any leftover stacks from previous runs...")
    cleanup_stacks()
    print("cleaning up any leftover buckets from previous runs...")
    cleanup_buckets()
    print("creating buckets...")
    create_buckets(temp_path)
    print("running tests...")
    execute_tests(tests)
