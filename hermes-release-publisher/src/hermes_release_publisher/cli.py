from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .repository import BucketMap, OssV2Backend, PublisherError, ReleasePublisher, content_addressed_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Alibaba Cloud OSS release publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan-artifact")
    plan.add_argument("file", type=Path)
    plan.add_argument("--namespace", choices=("artifacts", "evidence"), default="artifacts")

    for name in ("publish-artifact", "publish-release", "promote-channel", "publish-block"):
        command = subparsers.add_parser(name)
        _add_oss_arguments(command)
        command.add_argument("file", type=Path)
        if name == "publish-artifact":
            command.add_argument("--kind", required=True)
        elif name == "publish-release":
            command.add_argument("--release-id", required=True)
            command.add_argument("--generation", type=int, required=True)
        elif name == "promote-channel":
            command.add_argument("--channel", choices=("canary", "beta", "stable", "enterprise"), required=True)
            command.add_argument("--generation", type=int, required=True)
        elif name == "publish-block":
            command.add_argument("--generation", type=int, required=True)

    args = parser.parse_args()
    try:
        if args.command == "plan-artifact":
            source = args.file.resolve(strict=True)
            payload = source.read_bytes()
            import hashlib

            digest = hashlib.sha256(payload).hexdigest()
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "namespace": args.namespace,
                        "file": str(source),
                        "sha256": digest,
                        "size_bytes": len(payload),
                        "object_key": content_addressed_key(args.namespace, digest, source.name),
                    },
                    sort_keys=True,
                )
            )
            return 0

        buckets = _bucket_map(args)
        backend = OssV2Backend.from_environment(
            region=args.region,
            endpoint=args.endpoint,
            server_side_encryption=args.sse,
        )
        publisher = ReleasePublisher(backend, buckets)
        source = args.file.resolve(strict=True)
        if args.command == "publish-artifact":
            receipt = publisher.publish_artifact(source, kind=args.kind)
        elif args.command == "publish-release":
            receipt = publisher.publish_release_envelope(
                source,
                release_id=args.release_id,
                release_generation=args.generation,
            )
        elif args.command == "promote-channel":
            receipt = publisher.promote_channel(
                source,
                channel=args.channel,
                channel_generation=args.generation,
            )
        elif args.command == "publish-block":
            receipt = publisher.publish_block_manifest(source, block_generation=args.generation)
        else:
            raise PublisherError(f"unsupported command: {args.command}")
        print(json.dumps(receipt.to_json(), sort_keys=True))
        return 0
    except (PublisherError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"hermes_release_publisher_error: {error}") from error


def _add_oss_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--region", default=os.environ.get("HERMES_OSS_REGION"), required=os.environ.get("HERMES_OSS_REGION") is None)
    parser.add_argument("--endpoint", default=os.environ.get("HERMES_OSS_ENDPOINT"))
    parser.add_argument("--sse", choices=("AES256", "KMS"), default=os.environ.get("HERMES_OSS_SSE", "AES256"))
    parser.add_argument("--staging-bucket", default=os.environ.get("HERMES_OSS_STAGING_BUCKET"))
    parser.add_argument("--artifacts-bucket", default=os.environ.get("HERMES_OSS_ARTIFACTS_BUCKET"))
    parser.add_argument("--control-bucket", default=os.environ.get("HERMES_OSS_CONTROL_BUCKET"))
    parser.add_argument("--evidence-bucket", default=os.environ.get("HERMES_OSS_EVIDENCE_BUCKET"))


def _bucket_map(args: argparse.Namespace) -> BucketMap:
    values = {
        "staging": args.staging_bucket,
        "artifacts": args.artifacts_bucket,
        "control": args.control_bucket,
        "evidence": args.evidence_bucket,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PublisherError(f"missing OSS bucket configuration: {', '.join(missing)}")
    return BucketMap(**values)


if __name__ == "__main__":
    raise SystemExit(main())
