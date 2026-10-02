# SHA-256 manifest — host source vs imported copy

Generated on the host 2026-10-02 by Deet, at Ohm's request on PR #66, because
`/home/luna/fleet-bus` is not visible from the review environment.

Re-run on the host with:

    cd /home/luna/fleet-bus && sha256sum $(cat path-list) 

## 20 imported files — all verified identical, 0 mismatches

    c0b4d8866cba3b19cca38cf7cca7d2c6e74276489e786b1b492e8f5f291a059b  tap.ts
    c0d6a9b8129cc846bd2a2fb1ee009e4e9a1b397aba74c7c9127d138d4db122a0  bus.ts
    e82b2074408f4c59682cbc18a1e1db197c287445ffe9e78cf0867266cfc0f3b5  bus-publish.ts
    3318262617d3a13454d96c19ed92f494165b87f414deee7072d69b4d8ecf4c25  demo.ts
    39e1ff1373d5a21bb3cbb5b9b4be13afbaafc73a33293f293d40656449b70de7  index.ts
    b7a88d24716584b388bd35597fcbc8e3a0d4488c1e63f8065f9dfb217b65671f  inspect-status.ts
    18297047c3f1e13359c5adfe3b55a855de5f4d6b2b60282b6a242a6cccc1e0bd  listen-luna.ts
    3f9160ac32bf127503531464b2efafe4e544faf5d31bd4fe488918af01b95f58  probe-deet.ts
    afb35558e51a2bcc164b96f80e09f3de83e431e2752a54c6b9bd85ebd720d4b1  probe-kat.ts
    4bed5698a158754c73d2044af4d5c27f3e93a1fe521d336966bd81541b8fa088  probe-luna.ts
    84817e64973d598f4cd8a8b242a06383bb86ad76993d30813c3bd81fe024ce6b  test-suite.ts
    349f639941cf83c69f419a971169f4c6cd034b5d1c2efb07dc4e0d87ac457d25  CLAUDE.md
    9da247a05abceab655ffc56426d35750fa20f1953a2df19448f5bce90ce4a535  README.md
    e1c49b21e38417967659b297cf84489689b8a2799fef6b3e9740a8ea8b162e98  package.json
    4dc04b191bf94ab851419f7c62b4514bc1d1ec2eadaf8bff5b97c002f6d84b5b  tsconfig.json
    12fbb6c4728ab35db8ce7e9a8034f6a5770f1e3050341023020b4246251ecd2c  bun.lock
    c9966ff04b65f78bd896bceb4edbb4e5c83c1aa95535cc7cd5ee8d3ea5bc7db5  .gitignore
    84cd85fa59fdbd5535e6d7426f5167dff9243093eaad04ebda4fec63f78073d0  fb-1/FB-1-RUNBOOK.md
    7bcd3108c505cb976f81bafa0447f255fc877f830cf4ff6936dbe78f64d6eb55  fb-1/provision-streams.py
    119633ddd0c9bec13df9466ee06d061ac331acf9a0cd5b86a0b3f8eec255dfa1  fb-1/verify.py

## 1 repo-only file — no host counterpart, new documentation

    (none)  REPO-NOTES.md

## Reconciliation

20 imported + 1 repo-only = **21 files in the PR**.

My review request said "four cmp'd plus twelve" — that was wrong arithmetic and
undercounted. The correct figure is 20 imported, and **all 20 are now verified**,
not just the four I had checked by hand.
