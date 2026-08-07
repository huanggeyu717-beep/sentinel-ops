// 底图 (只画建筑与设施, 不含任何状态色 —— 状态是数据, 不是装饰)。
//
// 坐标系: viewBox 1000x600, 与数据库里的百分比一一对应 —— x% × 10, y% × 6。
// 底图与数据点必须在**同一个 <svg> 里**, 所以这里导出的是一组 SVG 子节点而不是
// 一张独立的图: 当 <img> 背景会让两者分属两套坐标系, 容器一缩放就错位。
//
// 分区口径: zone 是**责任区 (谁巡这一片)**, 不是商品品类。一个责任区里可以同时有
// 果蔬、乳制品、熟食 —— 它们共用一个负责人与一台数据采集板 (一区一台 Arduino)。
// 按品类分会分出十几个区, 而现场只有 3 台板子, 那样的分区落不了地。
//
// 布局照真实超市的"周边环形": 要制冷、要接水管的东西全部贴外墙与后墙, 中间空出来
// 放不怕水的干货货架, 后场用实墙隔开。
//
// **图上大部分设施是没有传感器的** —— 烘焙、熟食柜台、干货货架、收银都没有点。
// 这是刻意的: 5 个探头铺不满全店, 只盯有水管接进去的地方 (冷柜的冷凝水与除霜排水、
// 果蔬喷淋、制冰机的供水管与储冰槽、后场的走入式冷库与热水器)。
// 其中一个探头刻意放在制冰机的**下游**: 水顺着地面坡度与地漏跑, 不一定积在源头,
// 靠"哪几个先湿、哪几个后湿"反推源头 —— 这正是 multi_sensor_escalation 场景的意义。
//
// 版面约定: y 60–108 是标题带, 不放任何设备; 数据点与设备的文字标签都朝下,
// 所以每个点下方留了约 30 的净空。门一律用"墙线断开"表示, 不拿一条底色线去盖,
// 否则深色模式下会露出一条亮缝。
export const PLAN_ARIA_LABEL = '门店平面图: 一区生鲜、二区卖场中区、三区后场'

export default function PlanBase() {
  return (
    <>
      <rect width={1000} height={600} fill="var(--plan-surface, #fcfcfb)" />

      <g stroke="none">
        <rect x={40} y={60} width={280} height={480} fill="var(--plan-floor-a, #f2f1ec)" />
        <rect x={320} y={60} width={400} height={480} fill="var(--plan-floor-b, #f8f7f4)" />
        <rect x={720} y={60} width={240} height={480} fill="var(--plan-floor-c, #eae8e0)" />
      </g>

      {/* Zone 1 · 生鲜区: 贴左墙的冷柜 + 熟食柜台 + 果蔬 + 烘焙 */}
      <g stroke="var(--plan-line, #c9c8c1)" strokeWidth={1.5} fill="var(--plan-equip, #e3e1d8)">
        <rect x={52} y={135} width={46} height={150} rx={3} />
        <rect x={52} y={335} width={46} height={150} rx={3} />
        <rect x={150} y={128} width={160} height={46} rx={3} />
        <rect x={150} y={200} width={160} height={62} rx={8} />
        <rect x={150} y={285} width={160} height={62} rx={8} />
        <rect x={150} y={400} width={160} height={62} rx={8} />
      </g>
      <g stroke="var(--plan-line, #c9c8c1)" strokeWidth={1} opacity={0.7}>
        <line x1={52} y1={185} x2={98} y2={185} />
        <line x1={52} y1={235} x2={98} y2={235} />
        <line x1={52} y1={385} x2={98} y2={385} />
        <line x1={52} y1={435} x2={98} y2={435} />
      </g>

      {/* Zone 2 · 卖场中区: 冷冻岛柜 + 制冰机 + 干货货架 + 收银 + 入口 */}
      <g stroke="var(--plan-line, #c9c8c1)" strokeWidth={1.5} fill="var(--plan-equip, #e3e1d8)">
        <rect x={345} y={135} width={145} height={60} rx={4} />
        <rect x={555} y={135} width={145} height={60} rx={4} />
        <rect x={630} y={300} width={58} height={64} rx={4} />
        <rect x={345} y={495} width={90} height={26} rx={3} />
        <rect x={455} y={495} width={90} height={26} rx={3} />
      </g>
      <line x1={630} y1={322} x2={688} y2={322} stroke="var(--plan-line, #c9c8c1)" strokeWidth={1.5} />
      <g stroke="var(--plan-line, #c9c8c1)" strokeWidth={1.5} fill="var(--plan-rack, #dedcd4)">
        <rect x={345} y={300} width={230} height={28} rx={3} />
        <rect x={345} y={355} width={230} height={28} rx={3} />
        <rect x={345} y={410} width={230} height={28} rx={3} />
      </g>
      <g stroke="var(--plan-line, #c9c8c1)" strokeWidth={1} opacity={0.75}>
        <line x1={422} y1={300} x2={422} y2={328} />
        <line x1={499} y1={300} x2={499} y2={328} />
        <line x1={422} y1={355} x2={422} y2={383} />
        <line x1={499} y1={355} x2={499} y2={383} />
        <line x1={422} y1={410} x2={422} y2={438} />
        <line x1={499} y1={410} x2={499} y2={438} />
      </g>

      {/* Zone 3 · 后场: 走入式冷库 + 拖把池 + 热水器 + 收货门 */}
      <g stroke="var(--plan-line, #c9c8c1)" fill="var(--plan-equip, #e3e1d8)">
        <rect x={748} y={130} width={184} height={124} rx={3} strokeWidth={4} />
        <rect x={750} y={350} width={60} height={44} rx={3} strokeWidth={1.5} />
        <circle cx={890} cy={372} r={26} strokeWidth={1.5} />
      </g>
      {/* 冷库门口: rect 掏不出缺口, 只能用后场地面色补一段 —— 用 floor-c 而不是
          surface, 深色模式下才不会露出一条亮缝 */}
      <line x1={805} y1={254} x2={875} y2={254} stroke="var(--plan-floor-c, #eae8e0)" strokeWidth={7} />

      {/* 外墙: 入口与收货门处直接断开, 不靠覆盖 */}
      <g fill="none" stroke="var(--plan-line, #c9c8c1)" strokeLinecap="round">
        <path d="M40 540 V60 H960 V440" strokeWidth={3} />
        <path d="M960 505 V540 H700" strokeWidth={3} />
        <path d="M630 540 H40" strokeWidth={3} />
        <line x1={320} y1={60} x2={320} y2={540} strokeWidth={1.5} strokeDasharray="7 6" />
        <path d="M720 60 V410" strokeWidth={3} />
        <path d="M720 480 V540" strokeWidth={3} />
      </g>

      {/* 入口的门扇与开启弧线 */}
      <g fill="none" stroke="var(--plan-line, #c9c8c1)">
        <line x1={630} y1={540} x2={630} y2={472} strokeWidth={2.5} />
        <path d="M630 472 A68 68 0 0 1 698 540" strokeWidth={1.5} strokeDasharray="5 5" />
      </g>

      <g fontFamily="system-ui, -apple-system, sans-serif" fill="var(--plan-label, #52514e)">
        <text x={56} y={92} fontSize={18}>Zone 1 · 生鲜区</text>
        <text x={336} y={92} fontSize={18}>Zone 2 · 卖场中区</text>
        <text x={736} y={92} fontSize={18}>Zone 3 · 后场</text>
        <g fontSize={12.5} fill="var(--plan-label-muted, #78776f)" textAnchor="middle">
          <text x={75} y={210} transform="rotate(-90 75 210)">乳制品冷柜</text>
          <text x={75} y={410} transform="rotate(-90 75 410)">冷藏饮料柜</text>
          <text x={230} y={156}>肉类熟食柜台</text>
          <text x={230} y={236}>果蔬喷淋台</text>
          <text x={230} y={321}>果蔬台</text>
          <text x={230} y={436}>烘焙区</text>
          <text x={417} y={169}>冷冻岛柜</text>
          <text x={627} y={169}>冷冻岛柜</text>
          <text x={659} y={384}>制冰机</text>
          <text x={460} y={458}>干货与日用百货货架</text>
          <text x={590} y={514} textAnchor="start">收银</text>
          <text x={665} y={466}>入口</text>
          <text x={840} y={196}>走入式冷库</text>
          <text x={780} y={415}>拖把池</text>
          <text x={890} y={415}>热水器</text>
          <text x={944} y={478} transform="rotate(-90 944 478)">收货门</text>
        </g>
      </g>
    </>
  )
}
