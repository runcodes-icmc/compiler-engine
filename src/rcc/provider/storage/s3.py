from .storage_provider import StorageProvider

import boto3
import boto3.session
from botocore.config import Config
import os


class S3(StorageProvider):
    """
    Storage provider for accessing files in S3 buckets.
    """

    def __init__(self, cfg):
        s = boto3.session.Session(
            aws_access_key_id=cfg.s3["access_key"],
            aws_secret_access_key=cfg.s3["secret_key"],
            region_name=cfg.s3["region"],
        )
        s3 = s.resource("s3",
                        endpoint_url=cfg.s3["endpoint"],
                        config=Config(s3={"addressing_style": "path"}))
        self.commits_bucket = s3.Bucket(cfg.s3["commits_bucket"])
        self.outputfiles_bucket = s3.Bucket(cfg.s3["outputfiles_bucket"])
        self.files_bucket = s3.Bucket(cfg.s3["files_bucket"])
        self.compilation_files_dir = cfg.s3["compilation_files_dir"]
        self.cases_bucket = s3.Bucket(cfg.s3["cases_bucket"])

    def fetch_commit_file(self, commit, destination):
        self.commits_bucket.download_file(commit.aws_key, destination)

    def fetch_exercise_file(self, source, destination):
        source = os.path.join(self.compilation_files_dir, source)
        self.files_bucket.download_file(source, destination)

    def fetch_test_case_input_file(self, test_case, destination):
        self.cases_bucket.download_file(
            "{}/in".format(test_case.id), destination)

    def fetch_test_case_output_file(self, test_case, destination):
        self.cases_bucket.download_file(
            "{}/out".format(test_case.id), destination)

    def fetch_test_case_files(self, test_case, destination):
        for fname in test_case.files:
            key = "{}/files/{}".format(test_case.id, fname)
            dest_fname = os.path.join(destination, fname)
            self.cases_bucket.download_file(key, dest_fname)

    def store_commit_output(self, commit, commit_output_fname):
        key = os.path.basename(commit_output_fname)
        with open(commit_output_fname, "rb") as output_file:
            metadata = {
                "commitId": str(commit.id),
                "userEmail": commit.user_email,
                "exercise": str(commit.exercise_id),
                "offeringId": str(commit.offering_id),
                "realOfferingId": str(commit.real_offering_id),
                "courseId": str(commit.course_id),
            }
            obj = self.outputfiles_bucket.put_object(
                Body=output_file, Key=key, Metadata=metadata
            )
            obj.wait_until_exists()
