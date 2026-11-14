"""
Long-term copy of the audit log in S3, one gzipped JSON-lines file per run per day.

The database keeps the recent log for the API. S3 is the cheap, durable copy
you can query later with Athena or just download. Checked against moto only.
"""
import gzip
import json


class S3Archive:
    def __init__(self, bucket, region, prefix="audit", session=None):
        import boto3
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = (session or boto3.session.Session(region_name=region)).client("s3", region_name=region)

    def key(self, run, day, part=0):
        return f"{self.prefix}/run={run}/date={day}/events-{part:03d}.jsonl.gz"

    def put(self, run, events):
        """events: audit dicts. Groups them by day and writes one object per day. Returns the keys written."""
        by_day = {}
        for e in events:
            by_day.setdefault(e["ts"][:10], []).append(e)
        keys = []
        for day, rows in sorted(by_day.items()):
            part = self._next_part(run, day)
            body = gzip.compress("\n".join(json.dumps(r, default=str) for r in rows).encode())
            key = self.key(run, day, part)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ServerSideEncryption="AES256",
                               ContentType="application/x-ndjson", ContentEncoding="gzip")
            keys.append(key)
        return keys

    def _next_part(self, run, day):
        r = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{self.prefix}/run={run}/date={day}/")
        return r.get("KeyCount", 0)

    def read(self, run, day):
        out = []
        r = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{self.prefix}/run={run}/date={day}/")
        for obj in sorted(r.get("Contents", []), key=lambda o: o["Key"]):
            raw = gzip.decompress(self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()).decode()
            out += [json.loads(line) for line in raw.splitlines() if line]
        return out
