ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG ANSIBLE_CORE=2.17.14
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus openssh-server sudo python3 python3-venv \
      ca-certificates curl tar iproute2 procps && \
    rm -rf /var/lib/apt/lists/* && \
    (id ubuntu || useradd --uid 1000 --create-home --shell /bin/bash ubuntu) && \
    usermod --shell /bin/bash ubuntu && \
    printf 'ubuntu ALL=(ALL) NOPASSWD: ALL\nDefaults env_reset\n' > /etc/sudoers.d/ubuntu && \
    chmod 0440 /etc/sudoers.d/ubuntu && chmod 0700 /root && \
    chmod 0755 /home/ubuntu && \
    python3 -m venv /opt/ansible && \
    /opt/ansible/bin/pip install --no-cache-dir "ansible-core==${ANSIBLE_CORE}" PyYAML && \
    systemctl enable ssh

ENV container=docker
ENV PATH=/opt/ansible/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
STOPSIGNAL SIGRTMIN+3
USER ubuntu
CMD ["sleep", "infinity"]
