targetScope = 'resourceGroup'

param identityName string
param githubSubject string

resource githubIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: resourceGroup().location
}

resource githubCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: githubIdentity
  name: 'github-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: githubSubject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

output clientId string = githubIdentity.properties.clientId
output principalId string = githubIdentity.properties.principalId
