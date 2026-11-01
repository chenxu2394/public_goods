targetScope = 'resourceGroup'

param principalId string
param roleDefinitionId string
param roleAssignmentName string

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: roleAssignmentName
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionId
  }
}
