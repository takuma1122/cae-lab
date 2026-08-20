FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# The Ubuntu Docker base image excludes /usr/share/doc/* via dpkg to save
# space, but calculix-ccx-test ships its example .inp decks under that path.
# Re-include them (this file sorts after "excludes" so it wins).
RUN echo "path-include=/usr/share/doc/calculix-ccx-test/*" \
    > /etc/dpkg/dpkg.cfg.d/z-include-calculix-examples

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        calculix-ccx \
        calculix-ccx-test \
        gzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
CMD ["bash"]
