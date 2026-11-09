"""S3 is content storage only. Database references decide ownership and visibility."""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any, BinaryIO

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from backend.config import Settings
from backend.services.errors import AppError


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.slots = asyncio.Semaphore(4)
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(
                connect_timeout=5,
                read_timeout=20,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
                proxies={},
            ),
        )

    async def call[T](self, work: Callable[[], T]) -> T:
        async with self.slots:
            task = asyncio.create_task(asyncio.to_thread(work))
            try:
                # A cancelled request must not close a spool while its SDK thread still reads it.
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                finally:
                    raise
            except (BotoCoreError, ClientError, OSError):
                raise AppError(
                    503, "STORAGE_UNAVAILABLE", "文件存储暂时不可用，请稍后重试。"
                ) from None

    async def put(self, key: str, body: BinaryIO, size: int, mime: str) -> None:
        await self.call(
            partial(
                self.client.put_object,
                Bucket=self.settings.s3_bucket,
                Key=key,
                Body=body,
                ContentLength=size,
                ContentType=mime,
                IfNoneMatch="*",
            )
        )

    async def delete(self, key: str) -> None:
        if not key.startswith(self.settings.s3_prefix):
            raise ValueError("Object outside project prefix")
        await self.call(partial(self.client.delete_object, Bucket=self.settings.s3_bucket, Key=key))

    async def get(self, key: str, size: int) -> bytes:
        def read() -> bytes:
            response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
            body = response["Body"]
            try:
                data: bytes = body.read(size + 1)
                if len(data) != size:
                    raise OSError("Stored size mismatch")
                return data
            finally:
                body.close()

        return await self.call(read)

    async def initialize(self) -> None:
        def initialize() -> None:
            try:
                self.client.head_bucket(Bucket=self.settings.s3_bucket)
            except ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                    raise
                options = (
                    {}
                    if self.settings.s3_region == "us-east-1"
                    else {
                        "CreateBucketConfiguration": {"LocationConstraint": self.settings.s3_region}
                    }
                )
                self.client.create_bucket(Bucket=self.settings.s3_bucket, **options)

        await self.call(initialize)
