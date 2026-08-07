// 按角色隐藏操作入口只是体验优化 (SPEC-005 决策 2): 少给用户一个注定 403 的按钮。
// 真正的权限拦截在服务端 (SPEC-004 决策 6), 前端判定与它保持同一张能力表。
const TRANSITION_ROLES = new Set(['operator', 'manager', 'admin'])
const CROSS_ZONE_ROLES = new Set(['manager', 'admin'])

export const canTransitionIncidents = (roles: string[]): boolean =>
  roles.some((r) => TRANSITION_ROLES.has(r))

export const canCrossZoneAssign = (roles: string[]): boolean =>
  roles.some((r) => CROSS_ZONE_ROLES.has(r))

// 触发演练与事故流转是同一档 (operator 及以上, SPEC-005 决策 6)
export const canTriggerDrill = canTransitionIncidents
