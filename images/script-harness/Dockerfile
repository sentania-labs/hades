# Crucible worker base image: the e2e script harness (spec 18).
# The "harness" is a script implementing the adapter's launch contract: it reads
# the identity bundle, does the work, writes a CompletionClaimV1, and exits with
# the code the case wants. No model, no credential, no subscription, so the whole
# provider path can be proven in CI.
# Same shape as the three real base images: Debian slim by digest, uid 1000
# worker, git, curl, jq, nothing else. Build with images/build.sh script-harness.
ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:5ae3c39ebd15e229dcedd5cee596b2497182493d41ff162e824ba13fc1b2b867

FROM ${BASE_IMAGE}
# Declared here so it reaches the RUN below: useradd stamps /etc/shadow with
# the build day unless SOURCE_DATE_EPOCH is in its environment (S7).
ARG SOURCE_DATE_EPOCH
ARG HARNESS_VERSION=1.0.0
# The script is the harness, so its content is a build input. build.sh hashes
# pins.env, this Dockerfile, and itself into crucible.build_inputs; naming the
# script's sha256 here is what puts the script into that hash, and the check below
# is what makes a Dockerfile and a script that disagree fail the build instead of
# producing a second image under the same tag.
ARG HARNESS_SCRIPT_SHA256=4e41d2471ef8832110662225e5333c59190e93916f2fc9ab910a892a1f51db0d
ARG DEBIAN_SNAPSHOT=20260916T000000Z
ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG CURL_VERSION=7.88.1-10+deb12u15
ARG JQ_VERSION=1.6-2.1+deb12u2
ARG CA_CERTIFICATES_VERSION=20250419~deb12u1
ARG LAB_CA_SHA256=567f110bb80bb2179b44995c78c7a49a4149085a59f6acb73cf37c6503a0481a
RUN set -eu; \
    printf '%s\n' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "git=${GIT_VERSION}" \
        "curl=${CURL_VERSION}" \
        "jq=${JQ_VERSION}" \
        "ca-certificates=${CA_CERTIFICATES_VERSION}"; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt /var/log/apt /var/log/dpkg.log /var/log/alternatives.log; \
    rm -f /var/cache/ldconfig/aux-cache; \
    groupadd --gid 1000 worker; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/worker --shell /bin/bash worker; \
    find / -xdev -type f -perm /6000 -exec chmod a-s {} +
# --chmod on every COPY from the build context: without it the file keeps the mode of
# the checkout, which follows the cloner's umask (0664 under 002, 0644 under 022). The
# mode is in the layer but not in crucible.build_inputs, so two machines built the same
# tag with different digests (FDY-0082). build.sh refuses a context COPY without it.
COPY --chmod=0644 ca/sentania-lab-root.crt /usr/local/share/ca-certificates/sentania-lab-root.crt
RUN set -eu; \
    echo "${LAB_CA_SHA256}  /usr/local/share/ca-certificates/sentania-lab-root.crt" | sha256sum -c -; \
    update-ca-certificates
# The script is the harness. `touch` to the epoch for the same reason the real
# images pin their CLI mtimes: a COPY keeps the source file's mtime, and the
# build must be reproducible (S7).
COPY --chmod=0755 script-harness/harness.sh /usr/local/bin/crucible-script-harness
RUN set -eu; \
    echo "${HARNESS_SCRIPT_SHA256}  /usr/local/bin/crucible-script-harness" | sha256sum -c -; \
    touch -h -d "@${SOURCE_DATE_EPOCH}" /usr/local/bin/crucible-script-harness

ENV HOME=/home/worker \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
USER 1000:1000
WORKDIR /home/worker
