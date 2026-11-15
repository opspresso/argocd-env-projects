"""Publish one validated native build output; no repository code runs with these credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request
import zipfile

MAX_BYTES = 450 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("artifact repository redirects are refused")


def output_file(root, build):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("source directory is a symlink")
    root = root.resolve()
    path = root / build["context"] / build["output"]
    # Resolve parent links too; a symlinked output must never read a publisher credential.
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("artifact output leaves the source directory or is a symlink")
    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
        stream.close()
        raise ValueError("artifact must be a bounded regular file")
    try:
        with zipfile.ZipFile(stream) as archive:
            names = archive.namelist()
            if len(names) > 100000:
                raise ValueError("artifact has too many entries")
            if build["format"] == "jar":
                manifest = archive.getinfo("META-INF/MANIFEST.MF")
                if manifest.file_size > 65536 or b"Main-Class:" not in archive.read(manifest):
                    raise ValueError("JAR is not executable")
            elif build["format"] == "war":
                if not any(name.startswith("WEB-INF/") for name in names):
                    raise ValueError("WAR has no WEB-INF application")
            else:
                raise ValueError("unknown artifact format")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        return stream, info.st_size, digest
    except BaseException:
        stream.close()
        raise


def publish(root, build, base_url, project, pipeline, release, username, password):
    from urllib.parse import urlsplit
    base = urlsplit(base_url)
    if base.scheme != "https" or not base.netloc or base.username or base.password or base.query or base.fragment:
        raise ValueError("artifact repository must be an HTTPS URL without credentials")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", project) or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", pipeline):
        raise ValueError("invalid project or pipeline")
    if not re.fullmatch(r"[0-9a-f-]{36}", release):
        raise ValueError("invalid release")
    stream, size, digest = output_file(root, build)
    url = f"{base_url.rstrip('/')}/{project}/{pipeline}/{release}/app.{build['format']}"
    headers = {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}
    client = urllib.request.build_opener(NoRedirect)

    def remote_digest():
        try:
            response = client.open(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            raise RuntimeError(f"artifact read failed: HTTP {error.code}") from None
        with response:
            sha = hashlib.sha256()
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("remote artifact exceeds size limit")
                sha.update(chunk)
            return total, sha.hexdigest()

    try:
        existing = remote_digest()
        if existing is None:
            request = urllib.request.Request(url, data=stream, method="PUT", headers={**headers, "Content-Length": str(size), "Content-Type": "application/octet-stream"})
            try:
                with client.open(request, timeout=120) as response:
                    if response.status not in (200, 201, 204):
                        raise RuntimeError(f"artifact publication failed: HTTP {response.status}")
            except urllib.error.HTTPError as error:
                error.close()
                raise RuntimeError(f"artifact publication failed: HTTP {error.code}") from None
            existing = remote_digest()
        if existing != (size, digest):
            raise ValueError("published artifact does not match the build output")
        return {"format": build["format"], "url": url, "sha256": digest, "size": size}
    finally:
        stream.close()


def download(artifact, base_url, project, username, password, destination):
    url = artifact["url"]
    prefix = base_url.rstrip("/") + "/" + project + "/"
    if not url.startswith(prefix) or not re.fullmatch(r"https://[A-Za-z0-9.:-]+/[A-Za-z0-9._/-]+", url) or "/../" in url:
        raise ValueError("artifact is outside the project's configured repository")
    if not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]) or not isinstance(artifact["size"], int) or not 0 < artifact["size"] <= MAX_BYTES:
        raise ValueError("invalid artifact identity")
    headers = {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}
    client = urllib.request.build_opener(NoRedirect)
    try:
        response = client.open(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f"artifact download failed: HTTP {error.code}") from None
    sha = hashlib.sha256()
    total = 0
    with response, open(destination, "wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > artifact["size"]:
                raise ValueError("artifact exceeds recorded size")
            sha.update(chunk)
            output.write(chunk)
    if total != artifact["size"] or sha.hexdigest() != artifact["sha256"]:
        raise ValueError("artifact does not match its recorded SHA-256 and size")


if __name__ == "__main__" and sys.argv[1:] == ["download"]:
    download(json.loads(os.environ["ARTIFACT"]), os.environ["ARTIFACT_REPOSITORY"], os.environ["PROJECT"],
             os.environ["NEXUS_USERNAME"], os.environ["NEXUS_PASSWORD"], "/tmp/application-artifact")
elif __name__ == "__main__":
    artifact = publish("/work/src", json.loads(os.environ["RUNTIME"])["build"], os.environ["ARTIFACT_REPOSITORY"],
                       os.environ["PROJECT"], os.environ["PIPELINE"], os.environ["RELEASE"],
                       os.environ["NEXUS_USERNAME"], os.environ["NEXUS_PASSWORD"])
    Path("/tmp/artifact.json").write_text(json.dumps(artifact))
    print(f"Published {artifact['format']} sha256:{artifact['sha256']} ({artifact['size']} bytes)")
