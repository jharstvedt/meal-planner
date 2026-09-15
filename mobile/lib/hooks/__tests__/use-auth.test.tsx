/**
 * Tests for AuthProvider and useAuth hook.
 *
 * Verifies that on Web, AuthProvider is configured when Firebase is initialized
 * even without native Google OAuth client ID environment variables.
 */

import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.unmock('@/lib/hooks/use-auth');

import { AuthProvider, useAuth } from '../use-auth';

vi.mock('@/lib/firebase', () => ({
  isFirebaseConfigured: true,
  auth: {
    currentUser: null,
  },
}));

vi.mock('firebase/auth', () => ({
  onAuthStateChanged: vi.fn((_auth, callback) => {
    callback(null);
    return vi.fn();
  }),
  GoogleAuthProvider: vi.fn(),
  signOut: vi.fn(),
  signInWithPopup: vi.fn(),
  signInWithCredential: vi.fn(),
}));

vi.mock('expo-auth-session/providers/google', () => ({
  useAuthRequest: vi.fn(() => [null, null, vi.fn()]),
}));

const wrapper = ({ children }: { children: ReactNode }) => (
  <AuthProvider>{children}</AuthProvider>
);

describe('useAuth hook', () => {
  it('provides auth context without configuration errors on web', () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.signIn).toBeTypeOf('function');
    expect(result.current.signOut).toBeTypeOf('function');
  });
});
