#!/usr/bin/env bash
# Generate the gateway's TLS certificate.
#
# See documentation/specs/user-authentication-spec.md section 10.
#
# This is "Provider A" from that spec: a private certificate authority, for names
# that no public CA can ever issue for (.lan). It creates two things:
#
#   airlock-ca.pem    the root certificate. Install this once per device that
#                     will use airlock, and browsers stop warning. (A CA made
#                     before the rename is shoebox-ca.pem and is kept - below.)
#   fullchain.pem     the certificate nginx serves, plus the root, concatenated.
#   privkey.pem       its private key.
#
# The gateway reads fullchain.pem / privkey.pem from a fixed path and does not
# care how they got there. When this server moves to a public domain, replace
# those two files with ones from Let's Encrypt ("Provider B") and nothing in the
# nginx configuration changes.
#
# Usage
#   ./make-certs.sh                         # airlock.lan, the service names,
#                                           # and this machine's own LAN IPs
#   ./make-certs.sh lozaprada.org           # add another domain (and *.that)
#   ./make-certs.sh 192.168.1.16               # add an address explicitly
#   ./make-certs.sh --force                 # regenerate the leaf, keep the CA
#   ./make-certs.sh --new-ca                # start a new CA (every device must
#                                           # trust it again)
#   ./make-certs.sh --ca-label backup-host        # what to call this machine's CA
#   ./make-certs.sh --no-auto-ip            # only the addresses given above
#
# Regenerating the leaf keeps the CA, so devices that already trust the root do
# not need to be touched again. --new-ca (or deleting the CA files) starts over
# and every device must re-trust.
#
# ── Two things here are load-bearing on a second machine ─────────────────────
#
# ITS OWN IP IS A NAME. A deployment on the LAN is reached at https://192.168.1.16
# long before anyone gives it a DNS name, and a certificate that lists only
# .lan names is not valid for that address. The addresses of this machine go in
# the certificate by default for that reason. An extra name given on the command
# line that parses as an IPv4 address becomes an IP SAN, not a DNS one: browsers
# ignore a DNS entry that looks like an address.
#
# EVERY CA GETS ITS OWN NAME. This script used to call every CA it made
# "Shoebox Local CA", so two machines running it produced two DIFFERENT keys
# under one identical name. A browser that had trusted the first then met the
# second, looked the issuer up BY NAME, found the key it already had, and failed
# the signature check - reporting SEC_ERROR_BAD_SIGNATURE, which reads like a
# corrupted or forged certificate rather than the name collision it is. The CA
# now carries the machine's name, and both CA and leaf carry key identifiers so
# a browser can tell two same-named CAs apart by key rather than by name.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"

# A CA made before the rename to airlock (v5.6.4) is shoebox-ca.* and is used
# as it is: every device that trusts it would otherwise have to trust a new
# one. Only a machine with no CA yet gets airlock-ca.*. Looking for one name
# only would quietly make a NEW CA on every machine that has the old one.
CA_NAME=airlock-ca
if [[ ! -f "$CERT_DIR/airlock-ca.pem" && -f "$CERT_DIR/shoebox-ca.pem" ]]; then
  CA_NAME=shoebox-ca
fi
CA_KEY="$CERT_DIR/$CA_NAME.key"
CA_CRT="$CERT_DIR/$CA_NAME.pem"
LEAF_KEY="$CERT_DIR/privkey.pem"
LEAF_CRT="$CERT_DIR/leaf.pem"
FULLCHAIN="$CERT_DIR/fullchain.pem"

CA_DAYS=3650      # 10 years: this is a private root, re-trusting devices is the cost
LEAF_DAYS=825     # ~27 months, the maximum some clients accept

FORCE=0
NEW_CA=0
AUTO_IP=1
CA_LABEL=""
EXTRA_DOMAINS=()
EXTRA_IPS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)      FORCE=1 ;;
    --new-ca)     NEW_CA=1 ;;
    --no-auto-ip) AUTO_IP=0 ;;
    --ca-label)   CA_LABEL="${2:-}"; shift ;;
    -*)           echo "Unknown option: $1" >&2; exit 1 ;;
    # An IPv4 literal is an ADDRESS, not a domain. DNS:192.168.1.16 is ignored by
    # every browser, which is the trap this case exists to avoid.
    *.*.*.*)
      if [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        EXTRA_IPS+=("$1")
      else
        EXTRA_DOMAINS+=("$1")
      fi ;;
    *)            EXTRA_DOMAINS+=("$1") ;;
  esac
  shift
done

mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

# ── Subject Alternative Names ────────────────────────────────────────────────
# Every name a browser might use. The service names (backend., vision., …) are
# internal and stay on plain HTTP, but including them costs nothing and means
# curl-ing one directly over https also works.
SANS=(
  "DNS:airlock.lan"
  "DNS:*.airlock.lan"
  # The names before the rename (v5.6.4). The gateway redirects them to
  # airlock.lan, and a redirect over https needs the certificate to cover the
  # name it was asked for. Kept for good: old links are opened years later.
  "DNS:shoebox.lan"
  "DNS:*.shoebox.lan"
  "DNS:localhost"
  "IP:127.0.0.1"
)
for d in ${EXTRA_DOMAINS[@]+"${EXTRA_DOMAINS[@]}"}; do   # bash 3.2: empty array under set -u
  SANS+=("DNS:$d" "DNS:*.$d")
done

# This machine's own addresses. `ip` on Linux, `ifconfig` on macOS; loopback and
# link-local are dropped, and so is anything that is already listed.
host_ipv4s() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | awk '/inet /{print $2}'
  fi
}

ALL_IPS=()
for ip in ${EXTRA_IPS[@]+"${EXTRA_IPS[@]}"}; do ALL_IPS+=("$ip"); done
if [[ $AUTO_IP -eq 1 ]]; then
  while read -r ip; do
    [[ -z "$ip" || "$ip" == 127.* || "$ip" == 169.254.* ]] && continue
    ALL_IPS+=("$ip")
  done <<< "$(host_ipv4s)"
fi
for ip in ${ALL_IPS[@]+"${ALL_IPS[@]}"}; do
  case ",$(IFS=,; echo "${SANS[*]}")," in
    *",IP:$ip,"*) ;;                       # already there
    *) SANS+=("IP:$ip") ;;
  esac
done

SAN_STR="$(IFS=,; echo "${SANS[*]}")"

# ── Root CA ──────────────────────────────────────────────────────────────────
if [[ -f "$CA_KEY" && -f "$CA_CRT" && $NEW_CA -eq 0 ]]; then
  echo "Reusing existing CA: $CA_CRT"
  echo "  $(openssl x509 -in "$CA_CRT" -noout -subject | sed 's/^subject= *//')"
else
  # The machine's name, so this CA cannot be confused with the one another
  # deployment made - see the note at the top. hostname -s is the short name on
  # both macOS and Linux; when it is empty or useless, fall back to something
  # that is at least unique.
  if [[ -z "$CA_LABEL" ]]; then
    CA_LABEL="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
    [[ -z "$CA_LABEL" || "$CA_LABEL" == "localhost" ]] && CA_LABEL="$(date +%Y%m%d-%H%M%S)"
  fi
  [[ $NEW_CA -eq 1 && -f "$CA_CRT" ]] && echo "Replacing the existing CA (--new-ca)."
  echo "Creating root CA for '$CA_LABEL'..."
  openssl req -x509 -newkey rsa:4096 -sha256 -days "$CA_DAYS" -nodes \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=Airlock Local CA ($CA_LABEL)/O=Airlock Photos" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
  chmod 600 "$CA_KEY"
  rm -f "$CERT_DIR/$CA_NAME.srl"         # serials belong to the CA that is gone
  echo "  $CA_CRT"
  # A new CA cannot sign with the old leaf still on disk claiming to be current.
  FORCE=1
fi

# ── Leaf certificate ─────────────────────────────────────────────────────────
if [[ -f "$FULLCHAIN" && $FORCE -eq 0 ]]; then
  echo "Certificate already exists. Use --force to regenerate."
else
  echo "Creating server certificate for: $SAN_STR"
  CSR="$(mktemp)"; EXT="$(mktemp)"
  trap 'rm -f "$CSR" "$EXT"' EXIT

  openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "$LEAF_KEY" -out "$CSR" \
    -subj "/CN=airlock.lan/O=Airlock Photos" 2>/dev/null

  # authorityKeyIdentifier is what lets a browser holding two CAs of the same
  # name pick the right one by KEY rather than by name. Not keyid:always: a CA
# made before key identifiers were added has none, and OpenSSL then refuses to
# sign. Plain keyid falls back to the issuer name and serial for those.
  cat > "$EXT" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN_STR
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

  openssl x509 -req -in "$CSR" -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$LEAF_CRT" -days "$LEAF_DAYS" -sha256 -extfile "$EXT" 2>/dev/null

  # nginx wants the leaf first, then the issuer.
  cat "$LEAF_CRT" "$CA_CRT" > "$FULLCHAIN"
  chmod 600 "$LEAF_KEY"
  chmod 644 "$FULLCHAIN"
  echo "  $FULLCHAIN"
fi

echo
echo "Expires: $(openssl x509 -in "$FULLCHAIN" -noout -enddate | cut -d= -f2)"
# -ext is unsupported on LibreSSL (macOS), so read the SAN out of the text dump.
echo "Names:   $(openssl x509 -in "$FULLCHAIN" -noout -text | grep -A1 'Subject Alternative Name' | tail -1 | sed 's/^ *//')"
echo
echo "CA:      $(openssl x509 -in "$CA_CRT" -noout -subject | sed 's/^subject= *//')"
echo
echo "Trust THIS machine's root so browsers stop warning. Each machine has its"
echo "own, and a device that uses two of them needs both:"
echo "  macOS   sudo security add-trusted-cert -d -r trustRoot \\"
echo "            -k /Library/Keychains/System.keychain $CA_CRT"
echo "  iOS     AirDrop $(basename "$CA_CRT"), install the profile, then"
echo "          Settings > General > About > Certificate Trust Settings > enable"
echo "  Android Settings > Security > Encryption & credentials > Install a certificate > CA"
echo
echo "Then restart the gateway:  ./start-podman.sh"
