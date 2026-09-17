#!/bin/bash

# Logging function that frames a message with a border and adds a timestamp
log_message() {
    local message="$1"
    local combined_message="${message}"
    local length=${#combined_message}
    local border_length=$((length + 4))
    
    # Create the top border
    local border=""
    for ((i=0; i<border_length; i++)); do
        border="${border}─"
    done
    
    # Display the framed message with timestamp
    echo "╭${border}╮"
    echo "│ ${combined_message}   │"
    echo "╰${border}╯"
}

# Example usage:
# log_message "This is a test message"

log_success() {
    local message="$1"
    local timestamp=$(date +%H:%M:%S)
    local combined_message="[SUCCESS] ${timestamp}: ${message}"
    log_message "${combined_message}"
}

log_error() {
    local message="$1"
    local timestamp=$(date +%H:%M:%S)
    local combined_message="[ERROR] ${timestamp}: ${message}"
    log_message "${combined_message}"
}

log_warning() {
    local message="$1"
    local timestamp=$(date +%H:%M:%S)
    local combined_message="[WARNING] ${timestamp}: ${message}"
    log_message "${combined_message}"
}

log_info() {
    local message="$1"
    local timestamp=$(date +%H:%M:%S)
    local combined_message="[INFO] ${timestamp}: ${message}"
    log_message "${combined_message}"
}

# Return success only when the optional n8n-mcp profile is selected.
# Compose profile lists are comma-separated and may contain incidental spaces.
n8n_mcp_profile_enabled() {
    local profiles="${1:-}"
    profiles="${profiles//[[:space:]]/}"
    [[ ",$profiles," == *,n8n-mcp,* ]]
}

# Return a cheap, non-authoritative shape hint for n8n public API JWTs.
# Signature and issuer verification are intentionally not attempted here;
# issuance remains an n8n UI responsibility and the live API check below is
# authoritative. The payload audience rejects generic generated secrets early.
n8n_public_api_key_shape_hint() {
    local key="${1:-}"
    local header_segment=""
    local payload_segment=""
    local signature_segment=""
    local padded_payload=""
    local payload_json=""
    local expires_at=""
    local current_time=""

    [[ "$key" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]] || return 1
    IFS='.' read -r header_segment payload_segment signature_segment <<< "$key"

    padded_payload="${payload_segment//-/+}"
    padded_payload="${padded_payload//_//}"
    case $(( ${#padded_payload} % 4 )) in
        0) ;;
        2) padded_payload+="==" ;;
        3) padded_payload+="=" ;;
        *) return 1 ;;
    esac

    payload_json=$(printf '%s' "$padded_payload" | base64 --decode 2>/dev/null) || return 1
    [[ "$payload_json" =~ \"aud\"[[:space:]]*:[[:space:]]*\"public-api\" ]] || return 1

    # If an expiry claim is present, reject it locally before making a network
    # request. The public API remains authoritative for signature/issuer checks.
    if [[ "$payload_json" =~ \"exp\"[[:space:]]*:[[:space:]]*([0-9]+) ]]; then
        expires_at="${BASH_REMATCH[1]}"
        current_time=$(date +%s) || return 1
        (( expires_at > current_time )) || return 1
    fi

    return 0
}

# Enforce the n8n-mcp credential contract without ever echoing the supplied
# value. Deployments that do not select n8n-mcp intentionally remain unaffected.
require_n8n_mcp_api_key() {
    local profiles="${1:-}"
    local key="${2:-}"

    if ! n8n_mcp_profile_enabled "$profiles"; then
        return 0
    fi

    if [[ -z "$key" ]]; then
        log_error "The n8n-mcp profile requires N8N_API_KEY from n8n Settings -> API; it is never generated automatically."
        return 1
    fi

    if ! n8n_public_api_key_shape_hint "$key"; then
        log_error "N8N_API_KEY must be an n8n-issued public API JWT with audience public-api and a future expiry when n8n-mcp is enabled."
        return 1
    fi

    return 0
}

# Authoritatively validate the key against n8n's public API. This is kept out
# of generator/wizard checks so a configuration edit does not unexpectedly
# depend on network availability; the service runner calls it immediately
# before launch. Only the HTTP status is captured or reported.
require_n8n_mcp_api_key_live() {
    local profiles="${1:-}"
    local key="${2:-}"
    local n8n_url="${3:-}"
    local n8n_url_host=""
    local api_url=""
    local api_status=""

    if ! n8n_mcp_profile_enabled "$profiles"; then
        return 0
    fi

    if ! require_n8n_mcp_api_key "$profiles" "$key"; then
        return 1
    fi

    n8n_url_host="${n8n_url#https://}"
    if [[ "$n8n_url" != https://* || -z "$n8n_url_host" || "$n8n_url_host" == /* || \
          "$n8n_url" == *[[:space:]]* || "$n8n_url" == *"@"* || \
          "$n8n_url" == *"?"* || "$n8n_url" == *"#"* ]]; then
        log_error "N8N_URL must be configured as an HTTPS n8n public API base URL when n8n-mcp is enabled."
        return 1
    fi

    api_url="${n8n_url%/}/api/v1/workflows?limit=1"
    api_status=$(curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --connect-timeout 5 \
        --max-time 10 \
        --header "X-N8N-API-KEY: $key" \
        "$api_url" 2>/dev/null) || {
        log_error "N8N_API_KEY live validation failed due to an n8n API network error."
        return 1
    }

    if [[ "$api_status" =~ ^2[0-9][0-9]$ ]]; then
        return 0
    fi

    log_error "N8N_API_KEY live validation failed against the n8n API (HTTP ${api_status:-unknown})."
    return 1
}
