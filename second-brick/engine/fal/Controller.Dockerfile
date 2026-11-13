FROM scratch
ADD nova-runtime-root.tar.gz /
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends curl fontconfig bubblewrap libseccomp2 \
    && python -m pip install --no-cache-dir fal==1.84.1 \
    && rm -rf /var/lib/apt/lists/*
ADD controller-app.tar.gz /
COPY sandbox.py sandbox_engine.py verify_sandbox.py /opt/fal-worker/
WORKDIR /opt/fal-worker
ENTRYPOINT []
CMD []
