// Trusted Types + DOMPurify hardening for the Aakaar frontend.
//
// Intent (defense-in-depth alongside our Content-Security-Policy header):
//
// Trusted Types is a browser security primitive (Chromium-based engines) that
// turns dangerous DOM sinks — innerHTML, <script src>, etc. — into a hard
// error UNLESS the value flowing into them is a "trusted" object minted by a
// registered policy. Paired with a CSP `require-trusted-types-for 'script'`
// directive, this removes a whole class of DOM-XSS by making it impossible to
// assign an attacker-controlled string to a script-bearing sink.
//
// This module registers a single policy named "aakaar" that:
//   - createHTML(s):      runs the string through DOMPurify before it may be
//                         used as HTML, stripping script/iframe/object/embed/
//                         form while keeping the HTML + SVG profiles intact so
//                         xyflow (React Flow) and recharts keep rendering.
//   - createScriptURL(u): allows ONLY same-origin script URLs and throws on any
//                         cross-origin attempt, so a poisoned URL can never be
//                         loaded as executable script.
//
// We also try to register the same policy as the *default* policy so that even
// raw, un-wrapped sink assignments (e.g. inside third-party code) are forced
// through DOMPurify. Setting a default policy can fail if one already exists or
// if the page CSP disallows it, so that step is best-effort and swallowed.
//
// On non-Chromium browsers `window.trustedTypes` is undefined; there is nothing
// to register, so installTrustedTypesPolicy() is a safe no-op. DOMPurify still
// guards every place we render HTML through the exported sanitizeHtml() helper.

import DOMPurify from "dompurify";

// Minimal structural typings for the Trusted Types API. The DOM lib does not
// ship these in every TS target we build against, so we model just the surface
// we touch instead of pulling in an extra dependency.
// TODO(integration): replace with `trusted-types` lib types if it is added to
// the dependency set during wiring.
interface TrustedTypePolicyOptions {
  createHTML?: (input: string) => string;
  createScriptURL?: (input: string) => string;
  createScript?: (input: string) => string;
}

interface TrustedTypePolicyLike {
  readonly name: string;
}

interface TrustedTypePolicyFactoryLike {
  createPolicy: (
    name: string,
    options: TrustedTypePolicyOptions,
  ) => TrustedTypePolicyLike;
  readonly defaultPolicy?: TrustedTypePolicyLike | null;
}

declare global {
  interface Window {
    trustedTypes?: TrustedTypePolicyFactoryLike;
  }
}

// Shared DOMPurify configuration. SVG is intentionally allowed because xyflow
// and recharts emit inline SVG; we still forbid the obviously dangerous tags.
const PURIFY_CONFIG = {
  USE_PROFILES: { html: true, svg: true },
  FORBID_TAGS: ["script", "iframe", "object", "embed", "form"],
};

const POLICY_NAME = "aakaar";

// Guard so repeated calls (e.g. HMR remounts) do not attempt to re-register the
// named policy, which throws a DOMException on the second create.
let installed = false;

/**
 * Sanitize an untrusted HTML string for safe rendering.
 *
 * Use this anywhere a component needs to render HTML (e.g. via
 * dangerouslySetInnerHTML). It applies the same allow/deny configuration as the
 * Trusted Types policy so behavior is consistent whether or not the browser
 * supports Trusted Types.
 */
export function sanitizeHtml(s: string): string {
  // DOMPurify.sanitize returns a string when RETURN_TRUSTED_TYPE is not set.
  return DOMPurify.sanitize(s, PURIFY_CONFIG) as unknown as string;
}

/**
 * Register the "aakaar" Trusted Types policy (and, best-effort, the default
 * policy). Safe to call once at app bootstrap. No-op on browsers without
 * Trusted Types support, and idempotent across repeated invocations.
 */
export function installTrustedTypesPolicy(): void {
  if (installed) return;

  // Non-Chromium browsers: nothing to register. DOMPurify still protects every
  // render path through sanitizeHtml().
  if (typeof window === "undefined" || !window.trustedTypes) {
    installed = true;
    return;
  }

  const factory = window.trustedTypes;

  const policyOptions: TrustedTypePolicyOptions = {
    createHTML: (input: string): string => sanitizeHtml(input),
    createScriptURL: (input: string): string => {
      // Only allow same-origin script URLs; anything cross-origin is rejected
      // so a tampered URL can never be loaded as executable script.
      if (new URL(input, location.origin).origin === location.origin) {
        return input;
      }
      throw new Error(
        `Trusted Types: refused cross-origin script URL "${input}"`,
      );
    },
  };

  // Register the named policy. If this throws (e.g. a policy with this name was
  // already created, or CSP blocks it) we leave the app to fall back to
  // sanitizeHtml() rather than crashing bootstrap.
  try {
    factory.createPolicy(POLICY_NAME, policyOptions);
  } catch {
    // Named policy unavailable — best effort only.
  }

  // Best-effort: also become the default policy so even un-wrapped sink writes
  // (including in third-party code) are forced through DOMPurify. This may fail
  // if a default policy already exists or CSP forbids it; swallow and continue.
  try {
    factory.createPolicy("default", policyOptions);
  } catch {
    // Default policy already set or disallowed — acceptable.
  }

  installed = true;
}
