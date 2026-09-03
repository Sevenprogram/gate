import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Space,
  Tag,
  Typography,
} from "antd";
import {
  ACCOUNT_LABELS,
  api,
  type AccountKey,
  type AppConfig,
  type ProfileInfo,
} from "./api";

const { Text } = Typography;

const ACCOUNT_KEYS: AccountKey[] = ["spot", "futures_usdt", "futures_btc", "unified"];
const ID_PATTERN = /^[A-Za-z0-9_-]+$/;

interface AddForm {
  id: string;
  label: string;
  key: string;
  secret: string;
  accounts: AccountKey[];
}

/**
 * Add, edit, and remove accounts without touching profiles.toml by hand.
 *
 * Adding verifies the key against Gate before saving. Editing adjusts which
 * account types a profile exposes — the remedy for a key without permission
 * for some products. Deleting keeps local history; re-adding with the same id
 * reattaches it.
 */
export function ProfileManager({
  config,
  onClose,
  onChanged,
}: {
  config: AppConfig;
  onClose: () => void;
  /** Refetch /config so navigation reflects the change immediately. */
  onChanged: () => Promise<void>;
}) {
  const [list, setList] = useState<ProfileInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<ProfileInfo | null>(null);
  const [editChosen, setEditChosen] = useState<AccountKey[]>([]);
  const [form] = Form.useForm<AddForm>();

  const reload = async () => setList(await api.listProfiles());

  useEffect(() => {
    reload().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : String(e)),
    );
  }, []);

  const add = async (values: AddForm) => {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const created = await api.addProfile({
        id: values.id.trim(),
        label: values.label?.trim() ?? "",
        key: values.key?.trim() ?? "",
        secret: values.secret?.trim() ?? "",
        accounts: values.accounts,
      });
      form.resetFields();
      await reload();
      setNotice(created.note ?? "已添加。");
      await onChanged();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (profile: ProfileInfo) => {
    setError(null);
    setNotice(null);
    try {
      await api.deleteProfile(profile.id);
      await reload();
      setNotice(`已删除「${profile.label}」，历史保留在本地库里。`);
      await onChanged();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const saveEdit = async () => {
    if (!editing || editChosen.length === 0) {
      setError("至少选择一个账户类型。");
      return;
    }
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const result = await api.updateProfile(editing.id, editChosen);
      setEditing(null);
      await reload();
      setNotice(result.note ?? "已更新。");
      await onChanged();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="账户管理"
      open
      onCancel={onClose}
      footer={null}
      width={640}
      destroyOnHidden
    >
      {config.demo && (
        <Alert
          type="warning"
          showIcon
          message="演示模式下密钥验证会被跳过"
          description="填真实密钥也验证不了。要真正接上账户，先去掉 DASHBOARD_DEMO 再加。"
          style={{ marginBottom: 16 }}
        />
      )}
      {error && (
        <Alert type="error" showIcon message="出错了" description={error} style={{ marginBottom: 16 }} />
      )}
      {notice && (
        <Alert type="success" showIcon message="完成" description={notice} style={{ marginBottom: 16 }} />
      )}

      <Text type="secondary">已连接的账户</Text>
      <List
        size="small"
        dataSource={list ?? []}
        loading={list === null}
        renderItem={(profile) => (
          <List.Item
            actions={
              editing?.id === profile.id
                ? [
                    <Button key="save" type="primary" size="small" loading={busy} onClick={() => void saveEdit()}>
                      保存
                    </Button>,
                    <Button key="cancel" size="small" onClick={() => setEditing(null)}>
                      取消
                    </Button>,
                  ]
                : [
                    <Button
                      key="edit"
                      size="small"
                      onClick={() => {
                        setEditing(profile);
                        setEditChosen(profile.accounts);
                      }}
                    >
                      编辑
                    </Button>,
                    <Popconfirm
                      key="del"
                      title={`删除账户「${profile.label}」？`}
                      description={`本地历史保留，用同样的 id（${profile.id}）重新添加可以接回来。`}
                      onConfirm={() => void remove(profile)}
                      okText="删除"
                      cancelText="取消"
                    >
                      <Button size="small" danger>
                        删除
                      </Button>
                    </Popconfirm>,
                  ]
            }
          >
            <List.Item.Meta
              title={
                <Space size={8}>
                  {profile.label}
                  {profile.accounts.map((a) => (
                    <Tag key={a}>{ACCOUNT_LABELS[a] ?? a}</Tag>
                  ))}
                  {!profile.has_credentials && <Tag color="warning">无密钥</Tag>}
                </Space>
              }
              description={
                <Text code style={{ fontSize: 11 }}>
                  {profile.id}
                </Text>
              }
            />
            {editing?.id === profile.id && (
              <div style={{ width: "100%", marginTop: 8 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  取消勾选同步不了的类型（比如 key 没有权限的统一账户），页面就不会再因为它们报“数据不完整”。本地历史保留。
                </Text>
                <div style={{ marginTop: 8 }}>
                  <Checkbox.Group
                    options={ACCOUNT_KEYS.map((k) => ({
                      label: ACCOUNT_LABELS[k],
                      value: k,
                    }))}
                    value={editChosen}
                    onChange={(vals) => setEditChosen(vals as AccountKey[])}
                  />
                </div>
              </div>
            )}
          </List.Item>
        )}
      />

      <Typography.Paragraph
        type="secondary"
        style={{ fontSize: 12, marginTop: 16, marginBottom: 8 }}
      >
        添加账户：填 id、显示名、API key，勾选账户类型。添加前会拿这对密钥实际调一次
        Gate 做只读验证。密钥保存在本机 profiles.toml（已 gitignore）。key 请只勾
        <Text type="warning">只读</Text>
        权限，不要勾交易和提现。
      </Typography.Paragraph>

      <Form form={form} layout="vertical" onFinish={(v) => void add(v)} requiredMark={false}>
        <Space wrap size={12} style={{ display: "flex" }}>
          <Form.Item
            name="id"
            label="id（创建后不可改）"
            rules={[
              { required: true, message: "需要一个 id" },
              {
                pattern: ID_PATTERN,
                message: "只能是字母、数字、横线和下划线",
              },
            ]}
          >
            <Input placeholder="main" style={{ width: 150 }} />
          </Form.Item>
          <Form.Item name="label" label="显示名">
            <Input placeholder="主账户" style={{ width: 150 }} />
          </Form.Item>
          <Form.Item
            name="key"
            label="API key"
            rules={[{ required: !config.demo, message: "需要 key" }]}
          >
            <Input placeholder={config.demo ? "演示模式可留空" : ""} style={{ width: 220 }} />
          </Form.Item>
          <Form.Item
            name="secret"
            label="API secret"
            rules={[{ required: !config.demo, message: "需要 secret" }]}
          >
            <Input.Password style={{ width: 220 }} />
          </Form.Item>
        </Space>
        <Form.Item
          name="accounts"
          label="要看哪些账户类型"
          initialValue={["futures_usdt"]}
          rules={[{ required: true, message: "至少选择一个" }]}
        >
          <Checkbox.Group
            options={ACCOUNT_KEYS.map((k) => ({ label: ACCOUNT_LABELS[k], value: k }))}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          验证并添加
        </Button>
      </Form>
    </Modal>
  );
}
