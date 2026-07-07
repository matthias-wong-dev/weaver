# Fabric Capacity Control

You can pause, resume, and check an Azure Microsoft Fabric capacity directly
with Azure CLI. This repo includes a small Bash wrapper with this project's
Fabric capacity details baked in.

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
az fabric capacity show --resource-group rg-ilovegovernment-aue --capacity-name datawithoutguessing
az fabric capacity suspend --resource-group rg-ilovegovernment-aue --capacity-name datawithoutguessing
az fabric capacity resume --resource-group rg-ilovegovernment-aue --capacity-name datawithoutguessing
```

Add `--subscription <subscription-id>` if you do not want to rely on your
current `az account set` context.

## Bash Wrapper Usage

```sh
scripts/fabric_capacity.sh status
scripts/fabric_capacity.sh pause
scripts/fabric_capacity.sh resume
scripts/fabric_capacity.sh unpause
```

The wrapper defaults to resource group `rg-ilovegovernment-aue` and capacity
name `datawithoutguessing`. Set `FABRIC_SUBSCRIPTION_ID` if you do not want to
rely on your current `az account set` context.

## Azure RBAC

The signed-in identity needs these Azure RBAC actions on the Fabric capacity
resource:

```text
Microsoft.Fabric/capacities/read
Microsoft.Fabric/capacities/write
Microsoft.Fabric/capacities/suspend/action
Microsoft.Fabric/capacities/resume/action
```
