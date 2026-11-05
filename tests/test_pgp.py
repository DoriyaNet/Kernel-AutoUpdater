import io
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from kernel_autoupdater.builder import UpdateError, Updater
from kernel_autoupdater.config import Config
from kernel_autoupdater.kernelorg import KernelRelease
from kernel_autoupdater.runner import CommandRunner
from kernel_autoupdater.state import StateStore

GPG = shutil.which("gpg")


@unittest.skipUnless(GPG and os.name == "posix", "gpg is required for this test")
class PgpTests(unittest.TestCase):
    def test_verify_tarball_signature(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gnupg = root / "gnupg"
            gnupg.mkdir(mode=0o700)
            env = os.environ.copy()
            env["GNUPGHOME"] = str(gnupg)

            params = root / "keyparams"
            params.write_text(
                "%no-protection\n"
                "Key-Type: RSA\n"
                "Key-Length: 2048\n"
                "Name-Real: Test Signer\n"
                "Name-Email: test@example.invalid\n"
                "Expire-Date: 0\n"
                "%commit\n",
                encoding="utf-8",
            )
            subprocess.run(
                [GPG, "--batch", "--generate-key", str(params)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            tar_path = root / "linux-6.8.tar"
            with tarfile.open(tar_path, "w") as tar:
                data = b"all:\n"
                info = tarfile.TarInfo("linux-6.8/Makefile")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            signature = root / "linux-6.8.tar.sign"
            subprocess.run(
                [
                    GPG,
                    "--batch",
                    "--detach-sign",
                    "--armor",
                    "-o",
                    str(signature),
                    str(tar_path),
                ],
                check=True,
                env=env,
            )

            archive = root / "linux-6.8.tar.xz"
            with lzma.open(archive, "wb") as output, tar_path.open("rb") as source:
                shutil.copyfileobj(source, output)

            keyring = root / "keyring.gpg"
            with keyring.open("wb") as output:
                subprocess.run(
                    [GPG, "--batch", "--export"],
                    check=True,
                    env=env,
                    stdout=output,
                )

            config = Config(
                work_dir=root / "work",
                verify_pgp=True,
                pgp_keyring=keyring,
            )
            config.downloads_dir.mkdir(parents=True)
            updater = Updater(
                config,
                CommandRunner(),
                StateStore(config.state_path),
                current="6.7.0",
            )

            def fake_download(url, destination, **kwargs):
                shutil.copyfile(signature, destination)
                return Path(destination)

            updater.downloader = fake_download
            release = KernelRelease(
                version="6.8.0",
                moniker="custom",
                source="https://example.invalid/linux-6.8.tar.xz",
                pgp="https://example.invalid/linux-6.8.tar.sign",
            )

            old_home = os.environ.get("GNUPGHOME")
            os.environ["GNUPGHOME"] = str(gnupg)
            try:
                updater._verify_tarball_signature(release, archive)

                bad_tar = root / "bad.tar"
                bad_tar.write_bytes(tar_path.read_bytes() + b"tampered")
                bad_archive = root / "linux-6.8-bad.tar.xz"
                with lzma.open(bad_archive, "wb") as output, bad_tar.open("rb") as source:
                    shutil.copyfileobj(source, output)
                with self.assertRaises(UpdateError):
                    updater._verify_tarball_signature(release, bad_archive)
            finally:
                if old_home is None:
                    os.environ.pop("GNUPGHOME", None)
                else:
                    os.environ["GNUPGHOME"] = old_home


if __name__ == "__main__":
    unittest.main()

