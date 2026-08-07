from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .doctor import OssDoctorPolicyV1, OssRepositoryDoctor, OssV2DoctorBackend
from .repository import BucketMap, OssV2Backend, PublisherError, ReleasePublisher, content_addressed_key
from .signing import (
    ReleaseSigningError,
    build_release_trust_store,
    sign_control_payload,
    write_json_new,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Alibaba Cloud OSS release publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan-artifact")
    plan.add_argument("file", type=Path)
    plan.add_argument("--namespace", choices=("artifacts", "evidence"), default="artifacts")

    doctor = subparsers.add_parser("oss-doctor")
    _add_oss_arguments(doctor)
    doctor.add_argument("--required-cname", default=os.environ.get("HERMES_OSS_UPDATES_CNAME"))
    doctor.add_argument(
        "--expected-encryption",
        action="append",
        choices=("AES256", "KMS"),
        dest="expected_encryption",
        help="Allowed bucket SSE method; repeat to allow both AES256 and KMS.",
    )

    sign = subparsers.add_parser("sign-control")
    sign.add_argument("payload", type=Path)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--signed-at", required=True)
    sign.add_argument("--output", type=Path, required=True)

    trust = subparsers.add_parser("emit-trust-store")
    trust.add_argument("--private-key", type=Path, required=True)
    trust.add_argument("--key-id", required=True)
    trust.add_argument("--not-before", required=True)
    trust.add_argument("--not-after", required=True)
    trust.add_argument("--output", type=Path, required=True)

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

        if args.command == "sign-control":
            payload = _read_json_object(args.payload)
            envelope = sign_control_payload(
                payload,
                private_key_path=args.private_key.resolve(strict=True),
                key_id=args.key_id,
                signed_at=args.signed_at,
            )
            write_json_new(args.output, envelope)
            print(json.dumps({"schema_version": 1, "output": str(args.output.resolve()), "key_id": args.key_id}, sort_keys=True))
            return 0

        if args.command == "emit-trust-store":
            trust_store = build_release_trust_store(
                private_key_path=args.private_key.resolve(strict=True),
                key_id=args.key_id,
                not_before=args.not_before,
                not_after=args.not_after,
            )
            write_json_new(args.output, trust_store)
            print(json.dumps({"schema_version": 1, "output": str(args.output.resolve()), "key_id": args.key_id}, sort_keys=True))
            return 0

        buckets = _bucket_map(args)
        if args.command == "oss-doctor":
            policy = OssDoctorPolicyV1(
                expected_encryption=tuple(args.expected_encryption or ("AES256", "KMS")),
                required_cname=args.required_cname.lower() if args.required_cname else None,
            )
            backend = OssV2DoctorBackend.from_environment(region=args.region, endpoint=args.endpoint)
            report = OssRepositoryDoctor(backend, buckets, policy).run()
            print(json.dumps(report.to_json(), sort_keys=True))
            return 0 if report.passed else 12

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
    except (PublisherError, ReleaseSigningError, OSError, ValueError, json.JSONDecodeError) as error:
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


def _read_json_object(path: Path) -> dict[str, object]:
    source = path.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise ReleaseSigningError("control payload must be a regular non-symlink JSON file")
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReleaseSigningError("control payload root must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
