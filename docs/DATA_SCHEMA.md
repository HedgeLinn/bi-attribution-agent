# 数据 Schema — 零售电商星型模型

> 最后更新:2026-09-09

## 一、结构总览

1 事实表 + 4 维度表,下钻深度三级。

```
orders(事实) ──┬── dim_date(date_id)
                ├── dim_store(store_id)   → region → city → store
                ├── dim_product(product_id) → category → subcategory → sku
                └── dim_channel(channel_id) → 单级(线上/线下)
```

## 二、事实表 `orders`

粒度:每行 = 一个订单里的一件商品。

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | string | 订单号 |
| date_id | string | FK → dim_date |
| store_id | string | FK → dim_store |
| product_id | string | FK → dim_product |
| channel_id | string | FK → dim_channel |
| quantity | int | 件数 |
| amount | decimal | 销售额(GMV 口径) |
| discount | decimal | 优惠金额 |
| refund | decimal | 退款额(0 或正数) |

## 三、维度表

| 表 | 关键字段 | 下钻层级 |
|----|---------|---------|
| dim_store | store_id, store_name, city, region | region → city → store(三级) |
| dim_product | product_id, sku_name, subcategory, category | category → subcategory → sku(三级) |
| dim_date | date_id, date, year, month, day, week | 时间维度 |
| dim_channel | channel_id, channel_name | 单级 |

## 四、指标口径(语义层要定义)

| 指标 | 口径 | 依赖 |
|------|------|------|
| GMV | sum(amount) | amount |
| 销量 | sum(quantity) | quantity |
| 订单数 | count(distinct order_id) | order_id |
| 客单价 | GMV / 订单数 | 派生 |
| 退款率 | sum(refund) / GMV | 派生 |
| 优惠率 | sum(discount) / GMV | 派生 |

> 派生指标(客单价、退款率)是假设生成的关键依据:
> 用来区分「量掉了 / 价掉了 / 退款暴涨」。

## 五、埋点异常(验证归因用)

- **主异常**:`华东 → 上海 → 某旗舰门店`,6 月起断崖下跌;
  根因 = 某头部 SKU 下架 + 该 SKU 客单价权重高 → 该店客单价暴跌 + 销量微降。
- **干扰项**:同期 618 大促后自然回落,全量 GMV 都在跌;
  迫使 agent 不能停在「整体下跌」,必须下钻到门店层定位真凶。

## 六、待确认项

1. 两条下钻路径(地理 + 商品)都保留 —— 已确认。
2. 渠道维度是否多级 —— 暂定单级。
3. 时间粒度按天生成,覆盖 12 个月(足够环比/同比)。
4. 数据量级:50 万 ~ 100 万行(测 DuckDB 聚合,又不拖慢造数)。
