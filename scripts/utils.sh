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

# Validate the non-secret shape that n8n uses for public API JWTs. Signature
# verification is intentionally not attempted here; issuance remains an n8n UI
# responsibility. The payload audience prevents generic generated secrets from
# being accepted as an API credential.
n8n_public_api_key_is_jwt() {
    local key="${1:-}"
    local header_segment=""
    local payload_segment=""
    local signature_segment=""
    local padded_payload=""
    local payload_json=""

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
    [[ "$payload_json" =~ \"aud\"[[:space:]]*:[[:space:]]*\"public-api\" ]]
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

    if ! n8n_public_api_key_is_jwt "$key"; then
        log_error "N8N_API_KEY must be an n8n-issued public API JWT with audience public-api when n8n-mcp is enabled."
        return 1
    fi

    return 0
}
