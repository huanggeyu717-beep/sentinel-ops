# services 层约定:
# - 唯一可以碰数据库的层(routers 只做 IO 编排, engine 是纯函数)
# - Agent tools 与 MCP server 复用同一 service, 不各写一套 SQL
