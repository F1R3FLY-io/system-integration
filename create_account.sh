#!/bin/bash
# Script to create a test account on the PDS

# Configuration
PDS_URL="http://localhost:2583"
HANDLE="aidan-pyrofex.bsky.social"
EMAIL="aidan@pyrofex.net"
PASSWORD="password"

echo "Creating account on PDS..."
echo "Handle: $HANDLE"
echo "Email: $EMAIL"
echo

# Create account
RESPONSE=$(curl -s -X POST "$PDS_URL/xrpc/com.atproto.server.createAccount" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"$EMAIL\",
    \"handle\": \"$HANDLE\",
    \"password\": \"$PASSWORD\"
  }")

echo "Response:"
echo "$RESPONSE" | jq .

# Check if account was created successfully
if echo "$RESPONSE" | jq -e '.did' > /dev/null 2>&1; then
    DID=$(echo "$RESPONSE" | jq -r '.did')
    echo
    echo "✅ Account created successfully!"
    echo "DID: $DID"
    echo "Handle: $HANDLE"
    echo "Password: $PASSWORD"
    echo
    echo "You can now log in to F1R3SKY with:"
    echo "  Username: $HANDLE"
    echo "  Password: $PASSWORD"
else
    echo
    echo "❌ Failed to create account"
    ERROR=$(echo "$RESPONSE" | jq -r '.message // .error // "Unknown error"')
    echo "Error: $ERROR"
fi


