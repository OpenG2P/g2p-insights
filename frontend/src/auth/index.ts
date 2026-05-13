// Keycloak integration is deferred to post-MVP. This module is a no-op stub
// so the rest of the app can already reference `useUser()` / `getToken()`
// without conditional code. Swap this file with an oidc-client / keycloak-js
// implementation when adding auth.

export interface User {
  name: string;
  roles: string[];
}

export function useUser(): User {
  return { name: "anonymous", roles: ["insight.admin"] };
}

export function getToken(): string | null {
  return null;
}
