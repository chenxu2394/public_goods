#!/bin/bash
RESOURCE_GROUP="rg-public-goods"

# A list of regions that frequently have F1 capacity
REGIONS=("centralus" "westus" "westus2" "northeurope" "westeurope" "southcentralus" "eastus" "eastus2" "eastasia" "southeastasia" "japaneast" "japanwest" "uksouth" "ukwest" "australiaeast" "australiasoutheast" "brazilsouth" "canadacentral" "canadaeast" "francecentral" "francesouth" "germanywestcentral" "norwayeast" "norwaywest" "switzerlandnorth" "switzerlandwest" "uaenorth" "uaecentral" "koreacentral" "koreasouth")

for REGION in "${REGIONS[@]}"; do
    echo "Attempting to create F1 plan in: $REGION..."
    
    # Try to create the plan
    az appservice plan create \
        --name "asp-test-$REGION" \
        --resource-group $RESOURCE_GROUP \
        --sku F1 \
        --is-linux \
        --location $REGION \
        > /dev/null 2>&1
        
    # Check if the command succeeded
    if [ $? -eq 0 ]; then
        echo "✅ SUCCESS! $REGION has F1 capacity."
        echo "You can use '$REGION' in your main deployment script."
        
        # Clean up the test plan so you can use your real one
        az appservice plan delete --name "asp-test-$REGION" --resource-group $RESOURCE_GROUP --yes
        # break
    else
        echo "❌ FAILED. $REGION is full (Quota 0). Moving to next..."
    fi
done