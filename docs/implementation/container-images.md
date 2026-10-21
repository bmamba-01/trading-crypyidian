# Danh mục container image

**Ngày kiểm tra:** 21/10/2026. Image được khóa bằng digest OCI; đổi digest cần kiểm tra phiên bản, nguồn, kiến trúc và giấy phép trước khi cập nhật.

| Dịch vụ | Image/digest | Giấy phép/ghi chú | Nguồn |
| --- | --- | --- | --- |
| Ứng dụng | `python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83` | Python Software Foundation License; manifest đa kiến trúc. | [Python Official Images](https://github.com/docker-library/official-images/blob/master/library/python), [Python image versions](https://github.com/docker-library/python/blob/master/versions.json). |
| uv build tool | `ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b` | Image chỉ dùng ở build stage; uv có giấy phép MIT hoặc Apache-2.0. | [uv Docker image](https://docs.astral.sh/uv/guides/integration/docker/), [uv releases](https://github.com/astral-sh/uv/releases). |
| PostgreSQL | `postgres:18.1-bookworm@sha256:cc9f4143a8d2fa8cf3749d0cb4d26ecf2d53a77a2ac807e9ebd67ae22426221a` | PostgreSQL License. Dùng named volume và healthcheck `pg_isready`. | [PostgreSQL releases](https://www.postgresql.org/docs/release/), [PostgreSQL Official Images](https://github.com/docker-library/official-images/blob/master/library/postgres). |
| Object store | `dxflrs/garage:v2.4.1@sha256:9c96caa2612d3411acc5b0e6701fb238dbfba33e533a6d7d3d811a4b12d0d020` | AGPL-3.0; một node, replication factor 1 chỉ dành cho local. Dùng image gốc với cấu hình TOML chỉ đọc, `GARAGE_CONFIG_FILE` và `rpc_secret_file` theo hướng dẫn Garage; không yêu cầu shell trong container. Cần đánh giá pháp lý trước khi phân phối/đưa vào SaaS. | [Garage configuration](https://garagehq.deuxfleurs.fr/documentation/reference-manual/configuration/), [Garage quick start](https://garagehq.deuxfleurs.fr/documentation/quick-start/), [Garage source/license](https://github.com/deuxfleurs-org/garage), [Garage image tags/digests](https://hub.docker.com/r/dxflrs/garage/tags). |

Digest repository/image ở đây mô tả đầu vào dự kiến, không phải bằng chứng container đã pull/build. SBOM `sbom.cdx.json` hiện chỉ chứa dependency Python; image cần được xuất thành SBOM riêng trong CI trước khi phát hành.

MinIO không được chọn cho bootstrap này: tại ngày kiểm tra, Docker Hub đánh dấu repository `minio/minio` archived và tag mới nhất có thời gian cập nhật cũ. Garage có tag phát hành `v2.4.1` và tài liệu chính thức cập nhật; đánh đổi là giấy phép AGPL-3.0 và yêu cầu rà soát phân phối. Nguồn: [MinIO Docker Hub](https://hub.docker.com/r/minio/minio/tags), [Garage Docker Hub](https://hub.docker.com/r/dxflrs/garage/tags).
