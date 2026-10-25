========
umpfabot
========

umpfabot is a bot that keeps the umpfs of a Yocto BSP up to date with pull
requests, a bit like Dependabot does for other dependencies. It uses `umpf
<https://github.com/pengutronix/umpf>`_ and is a single Python script, which
is meant to run as a GitHub Actions workflow in the BSP repository.

Currently it has one command, ``stable-update``.

stable-update
=============

``umpfabot stable-update`` rebases the umpf of a BSP onto the newest
upstream stable release and opens a pull request for it. For each configured
entry it:

1. reads the ``series.inc`` (written by ``umpf format-patch --bb``) on the
   target branch to find the current *umpf-base*, e.g. ``v6.12.50``,
2. looks for the newest stable release of the same series, e.g. ``v6.12.53``,
3. stages everything locally:

   - ``umpf --identical --base=v6.12.53 --version=90 tag series.inc`` in its
     clone of the umpf repository. ``--identical`` uses exactly the topic
     commits recorded in the BSP, so only the base changes,
   - ``umpf format-patch --bb --update`` into the BSP,
   - the ``SRC_URI`` sha256sum of the new base tarball in the recipe,
   - ``make olddefconfig`` (or ``savedefconfig``) of the defconfig, in the
     configured SDK,
   - a single commit with all of that,

4. publishes, but only after everything was staged successfully: first the
   umpf tags (and the base tags if the umpf repository doesn't have them
   yet), then the BSP branch, then the pull request.

If the topic branches don't apply on the new base, there is no new umpf
tag. Instead, umpfabot opens a *draft* pull request that only updates
``UMPF_BASE`` and the ``SRC_URI`` checksum, so applying the patch stack fails
in ``do_patch``. Its description shows the conflicting topic, commit and
files, and how to resolve it. Developers can push to the branch.

The commit message contains a link to the diff between the old and the new
umpf and the commands to review the changes to the patch stack::

  git fetch https://github.com/acme/linux.git \
    tag v6.12.50 tag 6.12/acme/20260901-1 tag v6.12.53 tag 6.12/acme/20261017-90
  umpf range-diff 6.12/acme/20260901-1 6.12/acme/20261017-90

The pull request additionally shows the ``umpf range-diff`` summary, the
changes to the defconfig and links to the upstream changelogs.
If the umpf used by umpfabot has no ``range-diff`` command (see `umpf
version`_), the summary is left out and only the equivalent ``git
range-diff`` command is given.

Building and testing the result is left to the CI of the BSP.

Which releases are stable updates?
----------------------------------

umpfabot follows the convention of the ``upstream-stable-release-point``
class of oe-core (``devtool upgrade --stable``): a release is a stable
update if its first ``stable-version-parts`` (default: 2) version components
equal those of the current base. ``v6.12.53`` is a stable update of
``v6.12.50``, ``v6.18`` is not. ``stable-release-regex`` overrides this like
``UPSTREAM_STABLE_RELEASE_REGEX``. umpfabot doesn't need bitbake for this, it
applies the rule to the tags of the upstream repository.

The releases are looked up with ``git ls-remote`` in
``upstream.repository``. If that is unset, the tags of the umpf repository
itself are used, e.g. if another workflow mirrors the stable tags there.
Either way, umpfabot pushes the base tags to the umpf repository if they are
missing, so the umpf can be reproduced from there. A release is only
proposed once its tarball can be downloaded.

umpf tag versions
-----------------

umpf tags are named ``<name>/<date>-<version>``. To not collide with tags
created by developers, who usually start at ``-1``, umpfabot uses the first
free version starting at ``tag-version`` (default: 90).

Pending pull requests
---------------------

umpfabot considers a pull request *edited* once someone else pushed to its
branch, like Dependabot does. It never pushes to, closes or recreates edited
pull requests. It only comments on them, once per occasion.

When a newer release comes out while a pull request is open, the ``pending``
option decides what happens:

``stack`` (default)
  A new pull request is opened *on top of* the newest open one, i.e. with its
  branch as base. Pull requests that are being validated stay as they are and
  can be merged one after another. When a lower pull request is merged or
  closed, the ones above it are retargeted, either by GitHub when the branch
  is deleted or by umpfabot. GitHub offers to turn such a chain into a
  stacked pull request. If the newest pull request is still a draft, i.e. it
  has an unresolved conflict, nothing is stacked on top of it. If it is
  unedited, it is superseded instead, as below. If it is edited, umpfabot
  waits until it is ready for review.

``supersede``
  What Dependabot does: an unedited pull request is closed with "Superseded
  by #N" and a new one against the target branch replaces it. If it was
  edited, umpfabot comments that a newer release is available and waits
  until the pull request is merged or closed.

At most ``open-pull-requests-limit`` (default: 5) pull requests are open per
entry.

Other changes are handled as follows:

- If the umpf on the target branch changes, e.g. because a developer merged
  a new topic, unedited pull requests are recreated from the new umpf.
  Unedited pull requests that conflict with their base are recreated, reusing
  their umpf tag. So are stacked ones whose base branch moved. Merely new
  commits on the target branch don't cause a pull request to be recreated.
- If the target branch already is at the release of a pull request, the pull
  request is closed as no longer needed.
- Closing a pull request without merging it means: not this release. umpfabot
  won't propose it again, only newer ones. ``--force`` overrides this.

Setting it up
=============

1. Create a GitHub App for umpfabot under the organization or user that owns
   the repositories (*Settings* → *Developer settings* → *GitHub Apps* →
   *New GitHub App*). The workflow only uses it to get a token, nothing runs
   on a server of your own:

   - *GitHub App name*: must be unique on GitHub. The pull requests are
     opened by ``<app-slug>[bot]``, and the logo of the App (*Display
     information*) is their avatar.
   - *Homepage URL*: e.g. the BSP repository.
   - *Webhook*: uncheck *Active*, the App needs no events.
   - *Repository permissions*:

     - *Contents*: read and write (BSP branches, umpf tags)
     - *Pull requests*: read and write
     - *Workflows*: read and write, if the BSP or an umpf repository contains
       GitHub workflows (``.github/workflows/``), like barebox does. GitHub
       rejects pushes of an App without it if they add or change workflow
       files, e.g. the umpf tag on a release that updated the workflows, or a
       BSP branch recreated on a target branch with changed workflows.

   - *Where can this GitHub App be installed?*: *Only on this account*.

   On the *General* page of the App, note the *App ID* (not the *Client ID*)
   and generate a private key, which is downloaded as a ``.pem`` file.

   The ``GITHUB_TOKEN`` of the workflow is not sufficient: it cannot push to
   the umpf repository, and pull requests created with it don't trigger
   workflows, so the CI of the BSP would not build them.

2. Install the App (*Install App*) for the BSP and the umpf repositories,
   *Only select repositories* is enough.

3. Store the App ID as the variable ``UMPFABOT_APP_ID`` and the private key
   (the whole ``.pem`` file) as the secret ``UMPFABOT_PRIVATE_KEY`` of the
   BSP repository (*Settings* → *Secrets and variables* → *Actions*), which
   the example workflow uses, e.g. with the GitHub CLI::

     gh variable set UMPFABOT_APP_ID --body <app-id>
     gh secret set UMPFABOT_PRIVATE_KEY < <app-slug>.<date>.private-key.pem

4. Add the configuration as ``.github/umpfabot.yml`` to the BSP, see
   `examples/umpfabot.yml <examples/umpfabot.yml>`_ and below. Set
   ``author`` to the bot user of the App, so the commits show its logo as
   well::

     author: "<app-slug>[bot] <<user-id>+<app-slug>[bot]@users.noreply.github.com>"

   The user ID is the ``id`` of the bot user, not the App ID::

     curl -s 'https://api.github.com/users/<app-slug>%5Bbot%5D'

   No separate user account is needed for this.

5. Add the workflow, see `examples/umpfabot-stable-update.yml
   <examples/umpfabot-stable-update.yml>`_. It runs on a schedule and can be
   started manually from the *Actions* tab, optionally for one entry and a
   specific release, or as a dry run. Start it as a dry run first.

6. Make sure that

   - rulesets and branch protection allow the app to push ``umpfabot/**``
     branches in the BSP and to create tags in the umpf repository,
   - developers may (force-)push to ``umpfabot/**`` branches,
   - the CI of the BSP also runs for pull requests based on ``umpfabot/**``
     branches, if ``pending: stack`` is used,
   - the topic branches are still synchronized after a merge, e.g. with
     ``umpf --remote=<remote> --force push series.inc``. A stable update
     doesn't change the topic branches, but a resolved conflict does.

Configuration
=============

.. code-block:: yaml

  version: 1
  # identity for commits and tags, e.g. the bot user of the GitHub App, see
  # "Setting it up" (default: umpfabot <umpfabot@users.noreply.github.com>)
  author: "<app-slug>[bot] <<user-id>+<app-slug>[bot]@users.noreply.github.com>"

  umpf:
    repository: https://github.com/pengutronix/umpf.git
    version: "<branch, tag or commit>"   # default: the newest, with patches/

  stable-update:
    - id: linux-acme                # used in branch names
      series: recipes-kernel/linux/linux-acme/patches/series.inc   # required
      recipe: recipes-kernel/linux/linux-acme_6.12.bb  # with the SRC_URI checksum
      repository: https://github.com/acme/linux.git    # required, has the umpf tags
      compare-url: <template>       # default for github.com: .../compare/{old}..{new}

      upstream:
        repository: <url>           # default: 'repository'
        tag-prefix: v               # tag = <prefix><version>
        stable-version-parts: 2
        stable-release-regex: <regex on the version>
        changelog-url: <template>

      tarball:
        url: <template>             # download to compute the SRC_URI sha256sum
        checksum-name: <name>       # for SRC_URI[<name>.sha256sum]

      kconfig:
        defconfig: <path>           # required with kconfig
        savedefconfig: false        # minimal defconfig: make savedefconfig, not olddefconfig
        make-args: [LLVM=1]

      sdk:
        installer: <url or path>    # Yocto SDK installer, installed with -y -d
        sha256: <checksum of the installer>
        path: <path>                # or an SDK that is installed already
        environment-setup: environment-setup-*   # glob, relative to the SDK
        env:                        # set after sourcing environment-setup
          ARCH: arm64
          CROSS_COMPILE: aarch64-oe-linux-

      tag-version: 90
      target-branch: main           # default: the default branch
      pending: stack                # or supersede
      open-pull-requests-limit: 5
      labels: []
      reviewers: []                 # users or org/team
      assignees: []
      commit-message:
        prefix: "linux-acme: "      # default: "<id>: "
        signoff: false

The ``url`` and ``changelog-url`` templates can use ``{version}`` (e.g.
``6.12.53``), ``{tag}`` (``v6.12.53``), ``{major}`` (``6``), ``{minor}``
(``12``), ``{series}`` (``6.12``) and ``{id}``. ``compare-url`` uses
``{old}`` and ``{new}``, the old and new umpf tag. The checksum must be
assigned as ``SRC_URI[sha256sum]``, ``SRC_URI[<name>.sha256sum]`` or with an
inline ``;sha256sum=`` in the recipe.

``make olddefconfig`` runs as ``make -C <kernel> O=<tmp> olddefconfig`` with
a copy of the defconfig as ``.config``, and the resulting ``.config``
replaces the defconfig. With ``savedefconfig: true``, for a minimal
defconfig, ``make savedefconfig`` runs instead and its ``defconfig``
replaces the defconfig. It fills in the defaults of new options just like
``olddefconfig``. A minimal defconfig has no header comment with the version,
so it often stays the same in a stable update. ``sdk.env`` is applied after
sourcing the SDK environment, so it can override variables set there.
Without ``sdk``, the tools of the runner are used.

umpf version
------------

umpfabot doesn't use whatever umpf the runner has, but fetches it from
``umpf.repository``.

Without ``umpf.version``, it uses the newest umpf, the default branch of the
repository, and applies the patches in `patches/ <patches>`_ to it that are
not upstream yet. Currently, that is ``umpf: add range-diff command``.
Patches that are upstream already are skipped. If a patch doesn't apply,
umpfabot warns and uses the umpf without patches.

With ``umpf.version``, a branch, tag or commit, umpfabot uses exactly that
umpf, without patches. Pin a commit to only change how the umpf tags and
patch series are created when the configuration changes. Commits are fetched
once and cached in the work directory, branches and tags are resolved again
on every run. Quote the version, YAML reads some abbreviated hashes as
numbers.

``--umpf <path>`` uses a local umpf executable instead, e.g. to try changes
to umpf.

Running it locally
==================

::

  umpfabot -C <bsp> --dry-run stable-update [--id <id>] [--version <tag>]

``--dry-run`` stages everything, including the umpf tag and the BSP commit,
but neither pushes nor modifies pull requests. It still needs a token to
read the pull requests (``--token``, ``$UMPFABOT_TOKEN``, ``$GH_TOKEN`` or
``$GITHUB_TOKEN``). The repository is ``$GITHUB_REPOSITORY`` or derived from
the BSP remote. The work directory (``-w``, default ``~/.cache/umpfabot``)
contains a partial clone of the umpf repository, the umpf versions used,
installed SDKs and the checksums of downloads.

The tests in ``tests/`` run everything locally, with pull requests kept in a
JSON file or served by a minimal fake GitHub API. They need an umpf with
``range-diff``, e.g. with ``patches/`` applied, ``$UMPF`` or a checkout of
umpf next to this repository::

  UMPF=~/src/umpf/umpf pytest -v tests/

or, running the tests in parallel with pytest-xdist::

  pytest -n auto tests/
