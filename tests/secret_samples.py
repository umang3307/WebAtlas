"""Synthetic, non-real secrets for the findings tests.

Each is assembled from fragments at runtime, so no secret-shaped literal ever appears in the
source. That keeps the sample values out of git history and stops secret scanners (GitHub's push
protection, and WebAtlas's own detector run over this repo) from flagging the test fixtures —
while the assembled strings still exercise the real detection patterns.
"""

AWS_KEY = "AK" + "IA" + "1234567890" + "ABCDEF"              # AKIA + 16 chars
GITHUB_PAT = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz" + "0123456789"   # ghp_ + 36 chars
GCP_KEY = "AI" + "za" + "SyA1234567890" + "abcdefghijklmnopqrstuv"       # AIza + 35 chars
SLACK_TOKEN = "xo" + "xb" + "-123456789012-" + "abcdefghijklmnop"
STRIPE_KEY = "sk" + "_" + "live" + "_" + "abcdefghijklmnopqrstuvwx"      # sk_live_ + 24 chars
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----"
