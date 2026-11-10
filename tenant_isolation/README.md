# with_loader_criteria 租户隔离：适用范围、边界与回归

测试套件：`test_tenant_loader_criteria.py`（19 个用例，临时 SQLite 内存库，
两个租户 + 一条跨租户 address 行，逐入口断言返回对象/集合与实际发送的 SQL）。

运行：

```
.venv/bin/python -m pytest tenant_isolation/ -q
```

## 保护的入口（推荐配置下）

`include_aliases=False, propagate_to_loaders=True`（默认值）+  canonical
`do_orm_execute` 配方时，以下入口全部带租户谓词：

- 普通 `select(Entity)`（WHERE）
- 显式 `.join(relationship)` 的 ON 子句
- `joinedload`（ON 子句，目标表被别名化，如 `address_1.tenant_id`）
- `selectinload`（第二条 IN 查询的 WHERE）
- lazy load（WHERE，条件由实例携带的 option 传播而来）
- `Session.get()`：对象不在 identity map 时会发带租户谓词的 SQL，
  跨租户主键返回 `None`

## 不保护的入口（各自独立，不能互相代替作证）

- **`aliased()`**：默认 `include_aliases=False` 时完全不过滤，SQL 无 WHERE。
  需要 `include_aliases=True`。
- **Core `select(Table)` / `text()`**：`do_orm_execute` 会触发，但 Core 语句
  上没有 ORM 实体，criteria 静默不适用；返回全部租户的行。文本/Core 入口
  必须在应用层自行拼租户条件。
- **identity map**：同一 Session 内已加载的对象，`get()` 不再发 SQL、直接
  返回——租户切换后照样返回旧租户对象；已加载的集合同理，访问不再发 SQL。
- **过期实例的 refresh**：`expire_all()` 后的刷新走 column-load 路径，
  canonical 配方显式跳过 `is_column_load`，刷新 SQL 不带租户谓词
  （`test_expire_all_does_not_cut_scalar_refresh` 固定了这一泄漏）。

## Session 使用边界（应用侧必须遵守）

1. **一个 Session 只服务一个租户**。租户是请求级上下文，Session 也应是
   请求级的；不要在同一 Session 内切换租户。
2. 必须复用时，切换租户前 `session.close()`（或至少 `expunge_all()`）。
   `expire_all()` **不能**切断泄漏（见上：refresh 不带租户条件）。
3. 实例从加载时起就携带了当时的 criteria option：租户切换后，该实例的
   lazy (re)load 仍按**加载时**的租户值过滤
   （`test_lazy_load_after_tenant_switch_keeps_load_time_tenant`）。
   只有 expunge/close 让实例脱离 Session 才能摆脱这份冻结的 option。
4. 闭包租户值要**在事件回调里绑定为纯值局部变量**：

   ```python
   @event.listens_for(session, "do_orm_execute")
   def add_tenant_criteria(orm_execute_state):
       if (orm_execute_state.is_select
               and not orm_execute_state.is_column_load
               and not orm_execute_state.is_relationship_load):
           current_tenant_id = tenant_context.get()  # 每次执行取纯值
           orm_execute_state.statement = orm_execute_state.statement.options(
               with_loader_criteria(
                   TenantMixin,
                   lambda cls: cls.tenant_id == current_tenant_id,
               )
           )
   ```

   纯值闭包变量会被纳入编译缓存 key，租户变化即产生新 key，不会沿用旧
   SQL（`test_closure_tenant_change_does_not_go_stale`）。

## 反面配置（库会拒绝或会失守）

- **lambda 里调用函数**（如 `cls.tenant_id == current_tenant()`）：
  库直接抛 `InvalidRequestError`，不会静默缓存旧值
  （`test_function_call_inside_lambda_rejected`）。
- **闭包捕获 dict / ContextVar 等可变对象**：同样抛 `InvalidRequestError`
  （uncacheable closure variable）。按上面第 4 条改成纯值局部变量。
- **`propagate_to_loaders=False`**：顶层 select 仍过滤，但 joinedload 的
  ON 子句和 lazy load 都失去租户谓词（跨租户行泄漏）。注意不对称：
  selectinload 的第二条语句整体复制父语句 options，仍然过滤
  （`test_propagate_to_loaders_false`）。多租户场景不要设 False。
- **`include_aliases=False`（默认）+ `aliased()`**：见上，别名实体裸奔。

## 库级缺陷与最小修复（本仓库已附）

**缺陷**：`with_loader_criteria` 的 `entity_or_base` 使用未映射基类/mixin
（docstring 明确承诺支持的 "a super class of a particular set of mapped
classes"）时，只要 `include_aliases=False`（默认），criteria 被**静默丢弃**，
SELECT 与 ORM UPDATE/DELETE 都不加任何过滤——正是"只测一条普通 select"
测不出来的全局失守（本套件所有用例都以 mixin 为 criteria 目标，即为回归）。

根因：criteria 注册按 `_all_mappers()` 走 `__subclasses__()` 是正确的，但
应用端条件 `ae.include_aliases or ae.entity is ext_info` 对 root_entity 形式
（`ae.entity is None`）恒为 False。

修复（两处同一条件，保持 aliased 语义不变）：

- `lib/sqlalchemy/orm/context.py` `_get_extra_criteria`
- `lib/sqlalchemy/orm/bulk_persistence.py`（ORM UPDATE/DELETE 的同名逻辑）

```python
if (
    ae.include_aliases
    or ae.entity is ext_info
    or (ae.entity is None and not ext_info.is_aliased_class)
)
and ae._should_include(self)
```

回归测试（repo 套件内）：
`test/orm/test_relationship_criteria.py` 新增
`test_mixin_base_applies_w_default_include_aliases`、
`test_mixin_base_still_skips_aliased_by_default`、
`test_mixin_base_applies_to_orm_dml[update/delete]`。

验证：`test/orm` 全量 11609 passed / 169 skipped / 0 failed；
`tenant_isolation/` 19 passed。

## 遗留观察（未改动）

`propagate_to_loaders=False` 下 selectinload 仍过滤、joinedload/lazy 不过滤，
三者不对称（selectinload 的次级语句整体复制父语句 options，未检查该标志）。
docstring 对该参数的定义是"是否随实例携带 option"，selectinload 的行为可视为
符合字面定义，故仅在此记录，未改库行为。
