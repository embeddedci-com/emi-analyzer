package emi

import (
	"context"
	"errors"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

// S3Blob implements Blob against anything S3-compatible: a hosted object store in production,
// MinIO in the compose stack. Presigning works identically on both, which is the whole reason
// the compose stack can prove the production data path rather than merely imitate it.
type S3Blob struct {
	client  *s3.Client
	presign *s3.PresignClient
	bucket  string
	prefix  string
}

// DefaultKeyPrefix namespaces every object the EMI service touches.
//
// It exists so this service can share a bucket with the rest of the app instead of needing
// one of its own. The keys the handlers build -- uploads/<project>/… and runs/<run>/… --
// are generic enough to collide with anything else that reaches for the obvious name, so
// they are relative names within this namespace rather than bucket-absolute ones.
const DefaultKeyPrefix = "emi/"

// S3Config configures S3Blob.
type S3Config struct {
	Endpoint  string // e.g. http://minio:9000, or a hosted provider's regional endpoint
	Region    string
	Bucket    string
	AccessKey string
	SecretKey string

	// UsePathStyle is required for MinIO, and harmless for most hosted providers.
	UsePathStyle bool

	// PublicURL is the endpoint that *clients* (browsers, workers) use to reach storage,
	// when it differs from the one this server uses.
	//
	// In the local stack the server reaches MinIO at http://minio:9000 inside the compose
	// network while the browser reaches it at http://localhost:9000. The two cannot be
	// reconciled by rewriting the URL after signing: SigV4 includes the Host header in the
	// signed canonical request (X-Amz-SignedHeaders=host), so a swapped host produces a
	// 403 SignatureDoesNotMatch. Instead we keep a second client whose endpoint *is* the
	// public one and presign with that, so the signature matches the host the client will
	// actually send.
	//
	// Leave empty when storage has one address for everybody, as a hosted store does.
	PublicURL string

	// KeyPrefix is prepended to every key. Empty means DefaultKeyPrefix -- sharing the
	// app's bucket is the normal case, so it is what you get without asking.
	KeyPrefix string
}

// NewS3Blob builds a client and ensures the bucket exists.
func NewS3Blob(ctx context.Context, cfg S3Config) (*S3Blob, error) {
	if cfg.Bucket == "" {
		return nil, errors.New("emi: S3 bucket is required")
	}
	region := cfg.Region
	if region == "" {
		region = "us-east-1"
	}

	awsCfg, err := awsconfig.LoadDefaultConfig(ctx,
		awsconfig.WithRegion(region),
		awsconfig.WithCredentialsProvider(
			credentials.NewStaticCredentialsProvider(cfg.AccessKey, cfg.SecretKey, "")),
	)
	if err != nil {
		return nil, fmt.Errorf("emi: aws config: %w", err)
	}

	withEndpoint := func(endpoint string) func(*s3.Options) {
		return func(o *s3.Options) {
			if endpoint != "" {
				o.BaseEndpoint = aws.String(endpoint)
			}
			o.UsePathStyle = cfg.UsePathStyle
		}
	}

	// The operational client talks to storage from where this server sits.
	client := s3.NewFromConfig(awsCfg, withEndpoint(cfg.Endpoint))

	// The presigning client signs for the address the *client* will use. When PublicURL is
	// empty these are the same client configuration, which is the production case.
	presignSource := client
	if cfg.PublicURL != "" && cfg.PublicURL != cfg.Endpoint {
		presignSource = s3.NewFromConfig(awsCfg, withEndpoint(cfg.PublicURL))
	}

	b := &S3Blob{
		client:  client,
		presign: s3.NewPresignClient(presignSource),
		bucket:  cfg.Bucket,
		prefix:  normaliseKeyPrefix(cfg.KeyPrefix),
	}
	if err := b.ensureBucket(ctx); err != nil {
		return nil, err
	}
	return b, nil
}

// normaliseKeyPrefix accepts "emi", "emi/" and "/emi/" as the same folder, so the hot path
// is a concatenation rather than a fix-up on every key.
func normaliseKeyPrefix(prefix string) string {
	if prefix == "" {
		prefix = DefaultKeyPrefix
	}
	if prefix = strings.Trim(prefix, "/"); prefix == "" {
		return ""
	}
	return prefix + "/"
}

// objectKey maps a key from the handlers to the object it names in the bucket.
//
// The clean is not decoration. Artifact keys reach the server from the worker, and without
// it a key holding ".." segments would resolve above the prefix and let a worker read or
// write objects belonging to the rest of the app. After cleaning, no key can name anything
// outside its namespace, whatever it contains.
func (b *S3Blob) objectKey(key string) string {
	return b.prefix + strings.TrimPrefix(path.Clean("/"+key), "/")
}

func (b *S3Blob) ensureBucket(ctx context.Context) error {
	_, err := b.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(b.bucket)})
	if err == nil {
		return nil
	}
	_, err = b.client.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(b.bucket)})
	if err != nil {
		var owned *types.BucketAlreadyOwnedByYou
		var exists *types.BucketAlreadyExists
		if errors.As(err, &owned) || errors.As(err, &exists) {
			return nil
		}
		return fmt.Errorf("emi: create bucket %q: %w", b.bucket, err)
	}
	return nil
}

var _ BlobDeleter = (*S3Blob)(nil)

// Delete removes one object. S3 reports deleting something that is not there as success, which
// is what the caller wants: it is tidying up after a project, not asserting the object existed.
func (b *S3Blob) Delete(ctx context.Context, key string) error {
	_, err := b.client.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: aws.String(b.bucket), Key: aws.String(b.objectKey(key)),
	})
	return err
}

// DeletePrefix removes every object under a prefix, a page at a time.
func (b *S3Blob) DeletePrefix(ctx context.Context, prefix string) error {
	full := b.objectKey(prefix)
	if strings.TrimSuffix(full, "/") == strings.TrimSuffix(b.prefix, "/") {
		// The namespace itself is everything this service owns, including other projects.
		return errors.New("emi: refusing to delete the whole key prefix")
	}
	pager := s3.NewListObjectsV2Paginator(b.client, &s3.ListObjectsV2Input{
		Bucket: aws.String(b.bucket), Prefix: aws.String(full),
	})
	for pager.HasMorePages() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			return err
		}
		objs := make([]types.ObjectIdentifier, 0, len(page.Contents))
		for _, o := range page.Contents {
			objs = append(objs, types.ObjectIdentifier{Key: o.Key})
		}
		if len(objs) == 0 {
			continue
		}
		if _, err := b.client.DeleteObjects(ctx, &s3.DeleteObjectsInput{
			Bucket: aws.String(b.bucket),
			Delete: &types.Delete{Objects: objs, Quiet: aws.Bool(true)},
		}); err != nil {
			return err
		}
	}
	return nil
}

func (b *S3Blob) PresignGet(ctx context.Context, key string, ttl time.Duration) (string, error) {
	req, err := b.presign.PresignGetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(b.bucket), Key: aws.String(b.objectKey(key)),
	}, s3.WithPresignExpires(ttl))
	if err != nil {
		return "", err
	}
	return req.URL, nil
}

func (b *S3Blob) PresignPut(ctx context.Context, key, contentType string, ttl time.Duration) (string, error) {
	req, err := b.presign.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(b.bucket), Key: aws.String(b.objectKey(key)),
		ContentType: aws.String(contentType),
	}, s3.WithPresignExpires(ttl))
	if err != nil {
		return "", err
	}
	return req.URL, nil
}

// Stat is how the control plane verifies that an artifact a worker claims to have uploaded
// actually landed, without ever reading the bytes.
func (b *S3Blob) Stat(ctx context.Context, key string) (int64, string, error) {
	out, err := b.client.HeadObject(ctx, &s3.HeadObjectInput{
		Bucket: aws.String(b.bucket), Key: aws.String(b.objectKey(key)),
	})
	if err != nil {
		var nf *types.NotFound
		if errors.As(err, &nf) {
			return 0, "", ErrNotFound
		}
		return 0, "", err
	}
	var size int64
	if out.ContentLength != nil {
		size = *out.ContentLength
	}
	ct := ""
	if out.ContentType != nil {
		ct = *out.ContentType
	}
	return size, ct, nil
}
