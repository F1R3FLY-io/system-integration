#!/bin/bash
# Wrapper script to build f1r3sky Docker image with NPM authentication

set -e

# Check if NPM_TOKEN is set, if not try to load from .token.classic
if [ -z "${NPM_TOKEN}" ]; then
    if [ -f "../../.token.classic" ]; then
        NPM_TOKEN=$(cat ../../.token.classic)
    else
        echo "Error: NPM_TOKEN not set and .token.classic not found"
        exit 1
    fi
fi

# Backup the original .npmrc if it exists
if [ -f ".npmrc" ]; then
    cp .npmrc .npmrc.backup
fi

# Create a temporary .npmrc file with the token hardcoded
cat > .npmrc << EOF
# GitHub Packages registry for @f1r3fly-io scope
@f1r3fly-io:registry=https://npm.pkg.github.com

# Authentication for GitHub Packages (required even for public packages)
# Token is hardcoded for Docker build
//npm.pkg.github.com/:_authToken=${NPM_TOKEN}
EOF

# Build the Docker image
echo "Building Docker image with NPM authentication..."
docker build --network=host -t f1r3flyindustries/f1r3sky:latest .

# Restore the original .npmrc
if [ -f ".npmrc.backup" ]; then
    mv .npmrc.backup .npmrc
fi

echo "Docker build completed successfully"
