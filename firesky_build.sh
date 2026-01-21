#!/bin/bash


if [ "${NPM_TOKEN}" = "" ] ; then {
    if [ -f ../../.token.classic ] ; then {
        NPM_TOKEN=$(cat ../../.token.classic 2>/dev/null || echo '')
    } ; fi
} ; fi

if [ "${NVM_VERSION}" = "" ] ; then {
    if [ -f ~/.nvm/nvm.sh ] ; then {
        source ~/.nvm/nvm.sh
    } ; fi
} ; fi

nvm use 20
corepack enable
yarn install
yarn intl:build
yarn build-web
