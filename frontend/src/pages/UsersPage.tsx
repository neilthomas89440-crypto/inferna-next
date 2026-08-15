import { useState, type FormEvent } from "react";
import {
  useChangePassword,
  useCreateUser,
  useDeleteUser,
  useUsers,
} from "../api/hooks";
import { useAuth } from "../context/AuthContext";
import ConfirmDialog from "../components/ConfirmDialog";

export default function UsersPage() {
  const { user } = useAuth();
  const users = useUsers();
  const createUser = useCreateUser();
  const deleteUser = useDeleteUser();
  const changePassword = useChangePassword();

  const [creating, setCreating] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [resetting, setResetting] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");

  if (user?.role !== "admin") {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-6 text-slate-600">
        You need admin privileges to manage users.
      </div>
    );
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await createUser.mutateAsync({ username, password, role });
      setUsername("");
      setPassword("");
      setRole("user");
      setCreating(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed");
    }
  };

  const submitPassword = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await changePassword.mutateAsync({ id: resetting!, password: newPassword });
      setResetting(null);
      setNewPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Password change failed");
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">Users</h1>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
        >
          New user
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
      )}

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-4 py-2">Username</th>
              <th className="px-4 py-2">Role</th>
              <th className="px-4 py-2">Active</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-2 font-medium text-slate-700">{u.username}</td>
                <td className="px-4 py-2 text-slate-600">{u.role}</td>
                <td className="px-4 py-2 text-slate-600">{u.is_active ? "yes" : "no"}</td>
                <td className="px-4 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => setResetting(u.id)}
                    className="mr-3 text-sm text-slate-600 hover:underline"
                  >
                    Change password
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleting(u.id)}
                    className="text-sm text-red-600 hover:underline"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {creating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
            <h2 className="text-lg font-semibold text-slate-800">New user</h2>
            <form onSubmit={submit} className="mt-4 space-y-4">
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Username</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Password</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Role</span>
                <select
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                >
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setCreating(false)}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createUser.isPending}
                  className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {resetting && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
            <h2 className="text-lg font-semibold text-slate-800">Change password</h2>
            <form onSubmit={submitPassword} className="mt-4 space-y-4">
              <label className="block text-sm">
                <span className="font-medium text-slate-700">New password</span>
                <input
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setResetting(null);
                    setNewPassword("");
                  }}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={changePassword.isPending}
                  className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  Save
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete user"
          message="This user will lose access immediately."
          busy={deleteUser.isPending}
          onConfirm={() => {
            deleteUser.mutate(deleting, { onSettled: () => setDeleting(null) });
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
