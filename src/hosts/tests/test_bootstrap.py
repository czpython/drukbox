from hosts.service import _SANDBOX_BOOTSTRAP_SCRIPT as SCRIPT
from providers.setup_script import inject_authorized_keys, inject_secret_proxy_trust
from secret_proxy.certificates import CertificateAuthority


def test_bootstrap_script_fits_exe_setup_script_size_limit() -> None:
    # exe.dev's --setup-script is capped at 10 KiB. Encoded form (escape +
    # double-quote wrapping) is slightly larger; assert raw size first and
    # leave headroom for encoding overhead.
    assert len(SCRIPT.encode("utf-8")) < 9 * 1024


def test_full_exe_bootstrap_fits_setup_script_size_limit(tmp_path) -> None:
    authority = CertificateAuthority(tmp_path)
    script = inject_secret_proxy_trust(
        SCRIPT,
        ca_certificate=authority.certificate_pem,
        proxy_url="http://127.0.0.1:8781",
    )
    script = inject_authorized_keys(
        script,
        username="exedev",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    assert len(script.encode("utf-8")) < 10 * 1024


def test_bootstrap_script_has_bash_shebang() -> None:
    assert SCRIPT.splitlines()[0] == "#!/usr/bin/env bash"


def test_bootstrap_script_only_requires_tailscale_authkey() -> None:
    # Bootstrap's sole contract with the VM is TAILSCALE_AUTHKEY. Drukbox
    # detects the VM via the Tailscale API; the script does not call back.
    assert "TAILSCALE_AUTHKEY" in SCRIPT
    assert "REMOTE_HOST_ANNOUNCE_URL" not in SCRIPT
    assert "REMOTE_HOST_ANNOUNCE_TOKEN" not in SCRIPT


def test_bootstrap_script_does_not_call_drukbox() -> None:
    # No HTTP traffic from inside the box to the control plane.
    assert "curl" not in SCRIPT
    assert "Authorization" not in SCRIPT


def test_bootstrap_script_invokes_tailscale() -> None:
    assert 'run_privileged tailscale "${tailscale_args[@]}"' in SCRIPT
    assert "--authkey=$TAILSCALE_AUTHKEY" in SCRIPT
    assert "--ssh" in SCRIPT


def test_bootstrap_script_uses_privileged_service_operations() -> None:
    assert "sudo -n" in SCRIPT
    assert "run_privileged install -d" in SCRIPT
    assert "run_privileged systemctl enable --now tailscaled.service" in SCRIPT
