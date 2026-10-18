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
            [
                "bash",
                "ca-certificates",
                "curl",
                "sudo",
                "jq",
                "git",
                "gh",
                "openssh-client",
                "iptables",
                "iproute2",
            ]
        )
        # E2B's base adds openssh-server. Tailscale SSH would serve its host
        # keys, made at build time, from every sandbox.
        .run_cmd("DEBIAN_FRONTEND=noninteractive apt-get purge -y openssh-server", user="root")
        # E2B boots and snapshots the template, so tailscaled is disabled in the
        # step that installs it: each sandbox's bootstrap starts a fresh one.
        .run_cmd(
            [
                "curl -fsSL https://tailscale.com/install.sh -o /tmp/install-tailscale.sh",
                "sh /tmp/install-tailscale.sh",
                "rm /tmp/install-tailscale.sh",
                "systemctl disable tailscaled",
            ],
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
