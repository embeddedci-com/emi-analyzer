# Dev-only image for cmd/emi-server, the control plane on Postgres + S3.
#
# Used by deploy/docker-compose.yaml to test that storage path end to end. The local app is
# cmd/emi-local, which is built into the desktop app instead.
FROM golang:1.26-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/emi-server ./cmd/emi-server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/emi-server /emi-server
USER nonroot:nonroot
EXPOSE 8090
ENTRYPOINT ["/emi-server"]
