import argparse

from e2b import Template, default_build_logger

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Drukbox E2B template")
    parser.add_argument("--name", default="drukbox")
    arguments = parser.parse_args()
    template = (
        Template()
        .from_ubuntu_image("24.04")
        .apt_install(
            ["bash", "ca-certificates", "curl", "sudo", "jq", "git", "gh", "openssh-server"]
        )
        .run_cmd(
            "curl -fsSL https://tailscale.com/install.sh -o /tmp/install-tailscale.sh"
            " && sh /tmp/install-tailscale.sh && rm /tmp/install-tailscale.sh",
            user="root",
        )
    )
    Template.build(
        template,
        arguments.name,
        cpu_count=2,
        memory_mb=1024,
        on_build_logs=default_build_logger(),
    )
