from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast, override

import boto3
import boto3.session
from botocore.config import Config as BotoConfig

from ...config import Config
from ...model import Commit, TestCase
from .storage_provider import StorageProvider

if TYPE_CHECKING:
    from mypy_boto3_s3.service_resource import Bucket


class S3(StorageProvider):
    """
    Storage provider for accessing files in S3 buckets.
    """

    commits_bucket: Bucket
    outputfiles_bucket: Bucket
    files_bucket: Bucket
    cases_bucket: Bucket
    compilation_files_dir: str

    def __init__(self, cfg: Config) -> None:
        s3cfg = cast(dict[str, object], cfg.s3)
        s = boto3.session.Session(
            aws_access_key_id=str(s3cfg["access_key"]),
            aws_secret_access_key=str(s3cfg["secret_key"]),
            region_name=str(s3cfg["region"]),
        )
        # The boto3-stubs Session.resource overload set is only partially
        # typed here (only the s3 extra is installed), so the member access
        # itself reports as partially unknown even though the "s3" overload
        # resolves to S3ServiceResource.
        s3 = s.resource(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=str(s3cfg["endpoint"]),
            config=BotoConfig(s3={"addressing_style": "path"}),
        )
        self.commits_bucket = s3.Bucket(str(s3cfg["commits_bucket"]))
        self.outputfiles_bucket = s3.Bucket(str(s3cfg["outputfiles_bucket"]))
        self.files_bucket = s3.Bucket(str(s3cfg["files_bucket"]))
        self.compilation_files_dir = str(s3cfg["compilation_files_dir"])
        self.cases_bucket = s3.Bucket(str(s3cfg["cases_bucket"]))

    @override
    def fetch_commit_file(self, commit: Commit, destination: str) -> None:
        self.commits_bucket.download_file(commit.aws_key, destination)

    @override
    def fetch_exercise_file(self, source: str, destination: str) -> None:
        source = os.path.join(self.compilation_files_dir, source)
        self.files_bucket.download_file(source, destination)

    @override
    def fetch_test_case_input_file(self, test_case: TestCase, destination: str) -> None:
        self.cases_bucket.download_file("{}/in".format(test_case.id), destination)

    @override
    def fetch_test_case_output_file(
        self, test_case: TestCase, destination: str
    ) -> None:
        self.cases_bucket.download_file("{}/out".format(test_case.id), destination)

    @override
    def fetch_test_case_files(self, test_case: TestCase, destination: str) -> None:
        for fname in test_case.files:
            key = "{}/files/{}".format(test_case.id, fname)
            dest_fname = os.path.join(destination, fname)
            self.cases_bucket.download_file(key, dest_fname)

    @override
    def store_commit_output(self, commit: Commit, commit_output_fname: str) -> None:
        key = os.path.basename(commit_output_fname)
        with open(commit_output_fname, "rb") as output_file:
            metadata: dict[str, str] = {
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
