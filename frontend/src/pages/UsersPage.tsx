import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { Pagination } from "../components/Pagination";
import { usePagination } from "../lib/usePagination";
import type { UserRole } from "../api/types";

interface UserOut {
  id: number;
  username: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

const ROLE_LABELS: Record<UserRole, string> = {
  operator: "Оператор",
  engineer: "Инженер",
  observer: "Наблюдатель",
  admin: "Администратор",
  super_admin: "Супер-администратор",
};

const ROLES: UserRole[] = ["observer", "operator", "engineer", "admin", "super_admin"];

// ТЗ п.4.2.11 — управление пользователями: список, создание,
// редактирование роли, сброс пароля, блокировка.
export function UsersPage() {
  const [users, setUsers] = useState<UserOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [showCreate, setShowCreate] = useState(false);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newRole, setNewRole] = useState<UserRole>("operator");

  const [resetTarget, setResetTarget] = useState<UserOut | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [pendingBlock, setPendingBlock] = useState<UserOut | null>(null);
  const [pendingDelete, setPendingDelete] = useState<UserOut | null>(null);

  const pagination = usePagination(users ?? []);

  const load = useCallback(() => {
    setError(null);
    api
      .get<UserOut[]>("/api/users")
      .then(setUsers)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить пользователей"));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function handleCreate() {
    if (newUsername.trim().length < 3 || newPassword.length < 8) {
      setError("Логин — минимум 3 символа, пароль — минимум 8 символов");
      return;
    }
    setError(null);
    api
      .post("/api/users", { username: newUsername, password: newPassword, role: newRole })
      .then(() => {
        setShowCreate(false);
        setNewUsername("");
        setNewPassword("");
        setNewRole("operator");
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось создать пользователя"));
  }

  function handleRoleChange(user: UserOut, role: UserRole) {
    setError(null);
    api
      .put(`/api/users/${user.id}`, { role })
      .then(() => load())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось изменить роль"));
  }

  function handleConfirmToggleBlock() {
    if (!pendingBlock) return;
    const user = pendingBlock;
    setPendingBlock(null);
    setError(null);
    api
      .put(`/api/users/${user.id}`, { is_active: !user.is_active })
      .then(() => load())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось изменить статус пользователя"));
  }

  function handleConfirmDelete() {
    if (!pendingDelete) return;
    const target = pendingDelete;
    setPendingDelete(null);
    setError(null);
    api
      .del(`/api/users/${target.id}`)
      .then(() => {
        setNotice(`Пользователь «${target.username}» удалён.`);
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось удалить пользователя"));
  }

  function handleConfirmResetPassword() {
    if (!resetTarget) return;
    if (resetPassword.length < 8) {
      setError("Новый пароль — минимум 8 символов");
      return;
    }
    const user = resetTarget;
    setResetTarget(null);
    setError(null);
    api
      .post(`/api/users/${user.id}/reset-password`, { new_password: resetPassword })
      .then(() => {
        setResetPassword("");
        setNotice(`Пароль пользователя «${user.username}» сброшен.`);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось сбросить пароль"));
  }

  return (
    <div>
      <h1>Управление пользователями</h1>

      {error && <div className="error-message">{error}</div>}
      {notice && <p className="hint">{notice}</p>}

      <section className="card">
        <div className="card-header">
          <h2>Создание пользователя</h2>
          <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Отмена" : "Новый пользователь"}</button>
        </div>
        {showCreate && (
          <div className="filters">
            <label>
              Логин
              <br />
              <input value={newUsername} onChange={(e) => setNewUsername(e.target.value)} />
            </label>
            <label>
              Пароль
              <br />
              <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
            </label>
            <label>
              Роль
              <br />
              <select value={newRole} onChange={(e) => setNewRole(e.target.value as UserRole)}>
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
            </label>
            <button onClick={handleCreate}>Создать</button>
          </div>
        )}
      </section>

      {users === null && <p>Загрузка...</p>}

      {users !== null && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Логин</th>
              <th>Роль</th>
              <th>Статус</th>
              <th>Создан</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {pagination.pageRows.map((u) => (
              <tr key={u.id}>
                <td>{u.username}</td>
                <td>
                  <select value={u.role} onChange={(e) => handleRoleChange(u, e.target.value as UserRole)}>
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {ROLE_LABELS[r]}
                      </option>
                    ))}
                  </select>
                </td>
                <td>{u.is_active ? "активен" : "заблокирован"}</td>
                <td>{new Date(u.created_at).toLocaleDateString("ru-RU")}</td>
                <td>
                  <button onClick={() => setResetTarget(u)}>Сбросить пароль</button>{" "}
                  <button className="secondary" onClick={() => setPendingBlock(u)}>
                    {u.is_active ? "Заблокировать" : "Разблокировать"}
                  </button>{" "}
                  <button className="danger" onClick={() => setPendingDelete(u)}>
                    Удалить
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {users !== null && (
        <Pagination
          page={pagination.page}
          pageCount={pagination.pageCount}
          onPageChange={pagination.setPage}
          total={pagination.total}
          start={pagination.start}
          pageSize={pagination.pageSize}
        />
      )}

      {resetTarget && (
        <section className="card">
          <h2>Сбросить пароль: {resetTarget.username}</h2>
          <label>
            Новый пароль
            <br />
            <input type="password" value={resetPassword} onChange={(e) => setResetPassword(e.target.value)} />
          </label>{" "}
          <button onClick={handleConfirmResetPassword}>Сбросить</button>{" "}
          <button className="secondary" onClick={() => setResetTarget(null)}>
            Отмена
          </button>
        </section>
      )}

      {pendingBlock && (
        <ConfirmModal
          title={pendingBlock.is_active ? "Заблокировать пользователя" : "Разблокировать пользователя"}
          message={`Пользователь «${pendingBlock.username}» будет ${
            pendingBlock.is_active ? "заблокирован" : "разблокирован"
          }. Подтвердите операцию.`}
          confirmLabel={pendingBlock.is_active ? "Заблокировать" : "Разблокировать"}
          onConfirm={handleConfirmToggleBlock}
          onCancel={() => setPendingBlock(null)}
        />
      )}

      {pendingDelete && (
        <ConfirmModal
          title="Удалить пользователя"
          message={`Учётная запись «${pendingDelete.username}» будет удалена безвозвратно. Если за пользователем уже есть история в журнале аудита, удаление не пройдёт — используйте блокировку вместо удаления. Подтвердите операцию.`}
          confirmLabel="Удалить"
          onConfirm={handleConfirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
