# Fabric Capacity Control

You can pause, resume, and check an Azure Microsoft Fabric capacity directly
with Azure CLI. This repo includes a small Bash wrapper that reads the target
capacity from environment variables, so no environment-specific names are
baked into the repo.

## Setup

Install and sign in to Azure CLI:

```sh
brew install azure-cli
az login
az account set --subscription <subscription-id>
```

`brew install azure-cli` installs the `az` command on macOS.

The first `az fabric capacity ...` command automatically installs the
`microsoft-fabric` Azure CLI extension if it is not already installed.

## Azure CLI Usage

```sh
az fabric capacity show --resource-group <resource-group> --capacity-name <capacity-name>
az fabric capacity suspend --resource-group <resource-group> --capacity-name <capacity-name>
az fabric capacity resume --resource-group <resource-group> --capacity-name <capacity-name>
```

Add `--subscription <subscription-id>` if you do not want to rely on your
current `az account set` context.

## Bash Wrapper Usage

```sh
export FABRIC_RESOURCE_GROUP=<resource-group>
export FABRIC_CAPACITY_NAME=<capacity-name>

scripts/fabric_capacity.sh status
scripts/fabric_capacity.sh pause
scripts/fabric_capacity.sh resume
scripts/fabric_capacity.sh unpause
```

The wrapper requires `FABRIC_RESOURCE_GROUP` and `FABRIC_CAPACITY_NAME` and
exits with an error if either is unset. Set `FABRIC_SUBSCRIPTION_ID` as well if
you do not want to rely on your current `az account set` context.

## Azure RBAC

The signed-in identity needs these Azure RBAC actions on the Fabric capacity
resource:

```text
Microsoft.Fabric/capacities/read
Microsoft.Fabric/capacities/write
Microsoft.Fabric/capacities/suspend/action
Microsoft.Fabric/capacities/resume/action
```
