import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Pencil, Plus, Trash2, Wrench } from "lucide-react";

import type {
  CapabilityDefinitionResponse,
  Grant,
} from "@/api/types";
import { ErrorBanner } from "@/components/ErrorBanner";
import { formatISTDate } from "@/lib/datetime";

/**
 * Site- and capability-centric vault UI, shared by:
 *   - tenant-admin's /admin/grants page (using /admin/grants endpoints)
 *   - super-admin's tenant detail page (using /superuser/tenants/<id>/grants)
 *
 * The page hands in a `VaultApi` adapter; this component is unaware of
 * which endpoints back it. That keeps the UX identical for both
 * personas — Brahma sets up AARYA's vault using the same form a tenant
 * admin uses afterwards.
 */

export interface VaultApi {
  /** React-Query queryKey base; the component appends ["grants"] / etc. */
  queryKeyBase: readonly (string | number)[];
  listGrants: () => Promise<Grant[]>;
  createGrant: (input: {
    capability_ref: string;
    account_alias: string;
    secrets: Record<string, string>;
    input_defaults?: Record<string, unknown>;
  }) => Promise<Grant>;
  updateGrant: (
    id: string,
    patch: {
      account_alias?: string;
      secrets?: Record<string, string>;
      input_defaults?: Record<string, unknown>;
      enabled?: boolean;
    },
  ) => Promise<Grant>;
  deleteGrant: (id: string) => Promise<void>;
  /** Capabilities visible to this caller — tenant admins see only their
   * grant-eligible set; superusers see all. */
  listCapabilities: () => Promise<CapabilityDefinitionResponse[]>;
  capabilitiesQueryKey: readonly (string | number)[];
}

// ---------- top-level component -----------------------------------------

export function VaultSections({ api }: { api: VaultApi }) {
  const queryClient = useQueryClient();

  const grantsQ = useQuery({
    queryKey: [...api.queryKeyBase],
    queryFn: api.listGrants,
  });
  const capsQ = useQuery({
    queryKey: api.capabilitiesQueryKey,
    queryFn: api.listCapabilities,
  });

  const { siteCaps, sessionCaps } = useMemo(() => {
    const all = (capsQ.data ?? []).filter((c) => c.kind === "capability");
    return {
      siteCaps: all.filter(isSiteCapability),
      sessionCaps: all.filter((c) => c.secret_names.length === 0),
    };
  }, [capsQ.data]);

  const grants = grantsQ.data ?? [];
  const siteRefs = new Set(siteCaps.map((c) => c.ref));
  const sessionRefs = new Set(sessionCaps.map((c) => c.ref));
  const siteGrants = grants.filter((g) => siteRefs.has(g.capability_ref));
  const sessionGrants = grants.filter((g) =>
    sessionRefs.has(g.capability_ref),
  );

  const [showAddSite, setShowAddSite] = useState(false);
  const [editingSite, setEditingSite] = useState<Grant | null>(null);

  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: [...api.queryKeyBase] });

  return (
    <div>
      {grantsQ.error ? <ErrorBanner error={grantsQ.error} /> : null}

      <SitesSection
        siteCaps={siteCaps}
        siteGrants={siteGrants}
        api={api}
        onAdd={() => setShowAddSite(true)}
        onEdit={(g) => setEditingSite(g)}
        onChanged={refresh}
      />

      <div className="mt-10">
        <CapabilitiesSection
          sessionCaps={sessionCaps}
          sessionGrants={sessionGrants}
          api={api}
          onChanged={refresh}
        />
      </div>

      {showAddSite ? (
        <AddSiteModal
          siteCaps={siteCaps}
          existingAliases={new Set(siteGrants.map((g) => g.account_alias))}
          api={api}
          onClose={() => setShowAddSite(false)}
          onSaved={() => {
            setShowAddSite(false);
            refresh();
          }}
        />
      ) : null}

      {editingSite ? (
        <EditSiteModal
          grant={editingSite}
          api={api}
          onClose={() => setEditingSite(null)}
          onSaved={() => {
            setEditingSite(null);
            refresh();
          }}
        />
      ) : null}
    </div>
  );
}

// ---------- Sites -------------------------------------------------------

function SitesSection({
  siteCaps,
  siteGrants,
  api,
  onAdd,
  onEdit,
  onChanged,
}: {
  siteCaps: CapabilityDefinitionResponse[];
  siteGrants: Grant[];
  api: VaultApi;
  onAdd: () => void;
  onEdit: (g: Grant) => void;
  onChanged: () => void;
}) {
  const del = useMutation({
    mutationFn: (id: string) => api.deleteGrant(id),
    onSuccess: () => onChanged(),
  });

  return (
    <section>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold text-ink-50">
            <Globe size={14} className="text-signal-cyan" /> Sites
          </h2>
          <p className="mt-0.5 text-xs text-ink-500">
            One row per website your workflows log into. Username and
            password are stored in the vault and never returned by the API.
          </p>
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={onAdd}
          disabled={siteCaps.length === 0}
        >
          <Plus size={14} />
          Add site
        </button>
      </div>

      {siteGrants.length === 0 ? (
        <div className="card p-8 text-center">
          <Globe size={20} className="mx-auto mb-2 text-ink-700" />
          <div className="text-sm text-ink-300">No sites yet.</div>
          <div className="mt-1 text-xs text-ink-500">
            Add a site to let workflows log into it.
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {siteGrants.map((g) => (
            <SiteCard
              key={g.id}
              grant={g}
              onEdit={() => onEdit(g)}
              onDelete={() => del.mutate(g.id)}
              deleting={del.isPending}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function SiteCard({
  grant,
  onEdit,
  onDelete,
  deleting,
}: {
  grant: Grant;
  onEdit: () => void;
  onDelete: () => void;
  deleting: boolean;
}) {
  const url =
    typeof grant.input_defaults?.login_url === "string"
      ? grant.input_defaults.login_url
      : null;
  const display =
    typeof grant.input_defaults?.display_name === "string"
      ? grant.input_defaults.display_name
      : grant.account_alias;

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-ink-50">
              {display}
            </h3>
            {!grant.enabled ? (
              <span className="badge ring-amber-400/40 text-amber-300">
                paused
              </span>
            ) : null}
          </div>
          <div className="mt-0.5 truncate font-mono text-[11px] text-signal-cyan">
            {url ?? "no URL set"}
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            className="btn-ghost"
            onClick={onEdit}
            title="Edit site"
          >
            <Pencil size={13} />
          </button>
          <button
            type="button"
            className="btn-ghost text-rose-300 hover:bg-rose-500/10"
            onClick={() => {
              if (confirm(`Remove "${display}" from the vault?`)) onDelete();
            }}
            disabled={deleting}
            title="Remove site"
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="text-ink-500">Username</div>
          <div className="mt-0.5 font-mono text-ink-200">
            {grant.secret_names.includes("username") ? "••••••••" : "—"}
          </div>
        </div>
        <div>
          <div className="text-ink-500">Password</div>
          <div className="mt-0.5 font-mono text-ink-200">
            {grant.secret_names.includes("password") ? "••••••••" : "—"}
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-center justify-between border-t border-ink-700/60 pt-2 text-[11px] text-ink-500">
        <span>
          Reference key:{" "}
          <span className="font-mono text-ink-400">{grant.account_alias}</span>
        </span>
        <span>added {formatISTDate(grant.created_at)}</span>
      </div>
    </div>
  );
}

// ---------- Add Site ----------------------------------------------------

function AddSiteModal({
  siteCaps,
  existingAliases,
  api,
  onClose,
  onSaved,
}: {
  siteCaps: CapabilityDefinitionResponse[];
  existingAliases: Set<string>;
  api: VaultApi;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [capRef, setCapRef] = useState<string>(siteCaps[0]?.ref ?? "");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [aliasOverride, setAliasOverride] = useState<string | null>(null);
  const [showPwd, setShowPwd] = useState(false);

  const computedAlias = aliasOverride ?? slugify(name);
  const aliasTaken = existingAliases.has(computedAlias);
  const aliasInvalid = !!computedAlias && !validAlias(computedAlias);

  const create = useMutation({
    mutationFn: () =>
      api.createGrant({
        capability_ref: capRef,
        account_alias: computedAlias,
        secrets: { username, password },
        input_defaults: {
          login_url: url,
          display_name: name,
        },
      }),
    onSuccess: () => onSaved(),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!validAlias(computedAlias) || aliasTaken) return;
    create.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <form onSubmit={onSubmit} className="card w-full max-w-md p-5">
        <h3 className="mb-1 flex items-center gap-2 text-base font-semibold text-ink-50">
          <Globe size={14} className="text-signal-cyan" /> Add site
        </h3>
        <p className="mb-4 text-xs text-ink-400">
          Workflows will log into this site by name. Credentials are
          encrypted in the vault and never leave the server.
        </p>

        {siteCaps.length > 1 ? (
          <label className="mb-3 block">
            <span className="panel-title">Login type</span>
            <select
              className="input mt-1"
              value={capRef}
              onChange={(e) => setCapRef(e.target.value)}
              required
            >
              {siteCaps.map((c) => (
                <option key={c.ref} value={c.ref}>
                  {c.ref}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        <label className="mb-3 block">
          <span className="panel-title">Site name *</span>
          <input
            className="input mt-1"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setAliasOverride(null);
            }}
            placeholder="Acme Portal"
            required
            autoFocus
          />
          {name ? (
            <span className="mt-1 block text-[11px] text-ink-500">
              Reference key:{" "}
              <input
                className="ml-1 inline-block w-40 border-0 border-b border-dashed border-ink-700 bg-transparent px-0 py-0 font-mono text-[11px] text-ink-300 focus:border-accent-300 focus:outline-none focus:ring-0"
                value={computedAlias}
                onChange={(e) => setAliasOverride(e.target.value)}
                pattern="^[a-z][a-z0-9_-]*$"
              />
              {aliasInvalid ? (
                <span className="ml-2 text-rose-300">
                  must start with a letter; lowercase, digits, _ or -
                </span>
              ) : aliasTaken ? (
                <span className="ml-2 text-rose-300">already in use</span>
              ) : null}
            </span>
          ) : null}
        </label>

        <label className="mb-3 block">
          <span className="panel-title">URL *</span>
          <input
            className="input mt-1"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://acme.example.com/login"
            required
          />
        </label>

        <label className="mb-3 block">
          <span className="panel-title">Username *</span>
          <input
            className="input mt-1"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="admin"
            required
          />
        </label>

        <label className="mb-3 block">
          <span className="panel-title">Password *</span>
          <div className="relative">
            <input
              className="input mt-1 pr-16"
              type={showPwd ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button
              type="button"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-400 hover:text-ink-100"
              onClick={() => setShowPwd((v) => !v)}
            >
              {showPwd ? "hide" : "show"}
            </button>
          </div>
        </label>

        {create.error ? <ErrorBanner error={create.error} /> : null}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            className="btn-ghost"
            onClick={onClose}
            disabled={create.isPending}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={
              create.isPending ||
              !name ||
              !url ||
              !username ||
              !password ||
              !validAlias(computedAlias) ||
              aliasTaken
            }
          >
            {create.isPending ? "Saving…" : "Add site"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------- Edit Site ---------------------------------------------------

function EditSiteModal({
  grant,
  api,
  onClose,
  onSaved,
}: {
  grant: Grant;
  api: VaultApi;
  onClose: () => void;
  onSaved: () => void;
}) {
  const initialUrl =
    typeof grant.input_defaults?.login_url === "string"
      ? grant.input_defaults.login_url
      : "";
  const initialName =
    typeof grant.input_defaults?.display_name === "string"
      ? grant.input_defaults.display_name
      : grant.account_alias;

  const [name, setName] = useState(initialName);
  const [url, setUrl] = useState(initialUrl);
  const [enabled, setEnabled] = useState(grant.enabled);
  const [rotating, setRotating] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);

  const update = useMutation({
    mutationFn: () => {
      const patch: Parameters<typeof api.updateGrant>[1] = {};
      if (
        url !== initialUrl ||
        name !== initialName ||
        grant.input_defaults?.login_url == null
      ) {
        patch.input_defaults = {
          ...grant.input_defaults,
          login_url: url,
          display_name: name,
        };
      }
      if (enabled !== grant.enabled) patch.enabled = enabled;
      if (rotating) patch.secrets = { username, password };
      return api.updateGrant(grant.id, patch);
    },
    onSuccess: () => onSaved(),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    update.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink-950/80 backdrop-blur">
      <form onSubmit={onSubmit} className="card w-full max-w-md p-5">
        <h3 className="mb-1 flex items-center gap-2 text-base font-semibold text-ink-50">
          <Pencil size={14} /> Edit site
        </h3>
        <p className="mb-4 text-xs text-ink-400">
          Reference key:{" "}
          <span className="font-mono text-ink-300">{grant.account_alias}</span>{" "}
          (locked — workflows reference this key by name).
        </p>

        <label className="mb-3 block">
          <span className="panel-title">Site name</span>
          <input
            className="input mt-1"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </label>

        <label className="mb-3 block">
          <span className="panel-title">URL</span>
          <input
            className="input mt-1"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
        </label>

        <label className="mb-3 flex items-center gap-2 text-sm text-ink-200">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled — uncheck to pause without deleting.
        </label>

        <fieldset className="space-y-2 rounded-md border border-ink-700 p-3">
          <legend className="panel-title px-1">Credentials</legend>
          <label className="flex items-center gap-2 text-xs text-ink-300">
            <input
              type="checkbox"
              checked={rotating}
              onChange={(e) => setRotating(e.target.checked)}
            />
            Rotate username + password
          </label>
          <label className="block">
            <span className="text-[11px] text-ink-500">Username</span>
            <input
              className="input mt-0.5"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={rotating ? "New username" : "•••••••• (unchanged)"}
              disabled={!rotating}
              required={rotating}
            />
          </label>
          <label className="block">
            <span className="text-[11px] text-ink-500">Password</span>
            <div className="relative">
              <input
                className="input mt-0.5 pr-16"
                type={showPwd ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={rotating ? "New password" : "•••••••• (unchanged)"}
                disabled={!rotating}
                required={rotating}
              />
              {rotating ? (
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-400 hover:text-ink-100"
                  onClick={() => setShowPwd((v) => !v)}
                >
                  {showPwd ? "hide" : "show"}
                </button>
              ) : null}
            </div>
          </label>
        </fieldset>

        {update.error ? <ErrorBanner error={update.error} /> : null}

        <div className="mt-3 flex justify-end gap-2 pt-1">
          <button
            type="button"
            className="btn-ghost"
            onClick={onClose}
            disabled={update.isPending}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={update.isPending}
          >
            {update.isPending ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------- Capabilities (session-bound) -------------------------------

function CapabilitiesSection({
  sessionCaps,
  sessionGrants,
  api,
  onChanged,
}: {
  sessionCaps: CapabilityDefinitionResponse[];
  sessionGrants: Grant[];
  api: VaultApi;
  onChanged: () => void;
}) {
  const grantByRef = new Map(sessionGrants.map((g) => [g.capability_ref, g]));

  const enable = useMutation({
    mutationFn: (capRef: string) =>
      api.createGrant({
        capability_ref: capRef,
        account_alias: "default",
        secrets: {},
      }),
    onSuccess: () => onChanged(),
  });
  const disable = useMutation({
    mutationFn: (id: string) => api.deleteGrant(id),
    onSuccess: () => onChanged(),
  });

  return (
    <section>
      <div className="mb-3">
        <h2 className="flex items-center gap-2 text-base font-semibold text-ink-50">
          <Wrench size={14} className="text-accent-300" /> Capabilities
        </h2>
        <p className="mt-0.5 text-xs text-ink-500">
          Session-bound capabilities — they use whichever site is already
          logged in, so they don't need their own URL or credentials.
        </p>
      </div>

      {sessionCaps.length === 0 ? (
        <div className="card p-6 text-center text-sm text-ink-500">
          No session-bound capabilities registered.
        </div>
      ) : (
        <div className="card divide-y divide-ink-700/60 p-0">
          {sessionCaps.map((c) => {
            const grant = grantByRef.get(c.ref);
            const isEnabled = !!grant && grant.enabled;
            return (
              <div
                key={c.ref}
                className="flex items-center justify-between gap-4 px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="font-mono text-sm text-ink-100">{c.ref}</div>
                  <div className="mt-0.5 truncate text-xs text-ink-400">
                    {c.description}
                  </div>
                </div>
                <ToggleSwitch
                  on={isEnabled}
                  pending={enable.isPending || disable.isPending}
                  onChange={(next) => {
                    if (next && !grant) enable.mutate(c.ref);
                    else if (!next && grant) disable.mutate(grant.id);
                  }}
                />
              </div>
            );
          })}
          {enable.error || disable.error ? (
            <div className="px-4 pb-3 pt-2">
              <ErrorBanner error={enable.error ?? disable.error} />
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

function ToggleSwitch({
  on,
  pending,
  onChange,
}: {
  on: boolean;
  pending: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={() => !pending && onChange(!on)}
      disabled={pending}
      className={[
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition",
        on
          ? "border-emerald-300/60 bg-emerald-400/30"
          : "border-ink-700 bg-ink-800",
        pending ? "opacity-60" : "",
      ].join(" ")}
    >
      <span
        className={[
          "inline-block h-4 w-4 transform rounded-full bg-ink-50 transition-transform",
          on ? "translate-x-6" : "translate-x-1",
        ].join(" ")}
      />
    </button>
  );
}

// ---------- helpers -----------------------------------------------------

function isSiteCapability(c: CapabilityDefinitionResponse): boolean {
  return (
    c.secret_names.includes("username") && c.secret_names.includes("password")
  );
}

function slugify(s: string): string {
  const base = s
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  if (!base) return "";
  if (/^[0-9]/.test(base)) return `site-${base}`;
  return base;
}

function validAlias(s: string): boolean {
  return /^[a-z][a-z0-9_-]*$/.test(s) && s.length >= 1 && s.length <= 64;
}
