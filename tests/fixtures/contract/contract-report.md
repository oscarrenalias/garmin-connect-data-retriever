# Contract report

- Run: 20261022T131234Z
- Report version: 1
- Outcome: complete

## SPEC §2.3 items 1-6

| # | Item | Status |
| --- | --- | --- |
| 1 | Token field and Bearer header | confirmed |
| 2 | Pagination and ordering | confirmed |
| 3 | History restrictions | unknown |
| 4 | Detail and ZIP by sport | confirmed |
| 5 | Manual and multisport type keys | not_observed |
| 6 | Rate limits and missing files | not_observed |

## Token shape

| Field | Value |
| --- | --- |
| http_status | 200 |
| content_type | text/plain |
| header_names | cache-control, cf-cache-status, cf-ray, connection, content-encoding, content-type, date, pragma, referrer-policy, server, transfer-encoding, x-content-type-options, x-frame-options, x-xss-protection |
| body_length | 1146 |
| token_field | - |
| token_field_source | raw_body |
| jwt_like | yes |
| jwt | header_keys=alg, kid, typ, claim_names=client_ecosystem, client_id, client_type, exp, garmin_guid, iat, iss, jti, managed_status, revocation_eligibility, scope, alg=RS256, lifetime_s=86400 |
| status | confirmed |

## Header convention

| Field | Value |
| --- | --- |
| scheme | Bearer |
| probe_http_status | 200 |
| status | confirmed |

## Pagination

| Field | Value |
| --- | --- |
| inventory_status | complete |
| page_size_requested | 99 |
| page_size_effective | 49 |
| page_size_reductions | from=99, to=49, start=0 |
| page_size_reduction_count | 1 |
| server_error_pages | 1 |
| pages | item_count=49, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:40 GMT, content-type=application/json;charset=UTF-8, item_count=49, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:41 GMT, content-type=application/json;charset=UTF-8, item_count=49, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:43 GMT, content-type=application/json;charset=UTF-8, item_count=49, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:44 GMT, content-type=application/json;charset=UTF-8, item_count=13, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:45 GMT, content-type=application/json;charset=UTF-8, item_count=0, http_status=200, headers=date=Thu, 22 Oct 2026 13:12:47 GMT, content-type=application/json;charset=UTF-8, content-length=2 |
| duplicate_id_count | 0 |
| same_start_time_count | 0 |
| total_distinct | 209 |
| status | confirmed |

Server returned 5xx for pages of 99; effective page size 49.
Search page requests that returned 5xx: 1.

## Page size cap

| Field | Value |
| --- | --- |
| status | confirmed |
| probes | requested=200, returned=-, error=HTTP 500, note=server error; see pagination.page_size_reductions, requested=1000, returned=-, error=HTTP 500, note=server error; see pagination.page_size_reductions |

## Ordering

| Field | Value |
| --- | --- |
| stable | yes |
| differing_positions | 0 |
| status | confirmed |

## Inventory counts

| Field | Value |
| --- | --- |
| by_type_key | cycling=2, hiit=19, indoor_cardio=41, lap_swimming=79, mobility=31, open_water_swimming=8, running=24, strength_training=4, walking=1 |
| by_parent_type_id | 17=27, 26=87, 29=95 |
| child_count | 0 |
| parent_count | 0 |
| manual_count | 0 |
| oldest_month | 2026-03 |
| newest_month | 2026-10 |
| owner_id_constant | yes |
| status | confirmed |

## Samples

| name | category | type_key | summary | workouts | original_file |
| --- | --- | --- | --- | --- | --- |
| pool_swim-1 | pool_swim | lap_swimming | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2458, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:09 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2, etag_present=no, last_modified_present=no, headers=content-length=2, content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:10 GMT, item_count=0 | http_status=200, content_type=application/x-zip-compressed, byte_length=39623, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:11 GMT, manifest=files/pool_swim-1.manifest.json |
| pool_swim-2 | pool_swim | lap_swimming | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2450, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:13 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2, etag_present=no, last_modified_present=no, headers=content-length=2, content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:14 GMT, item_count=0 | http_status=200, content_type=application/x-zip-compressed, byte_length=43204, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:15 GMT, manifest=files/pool_swim-2.manifest.json |
| open_water_swim-1 | open_water_swim | open_water_swimming | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2724, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:17 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2, etag_present=no, last_modified_present=no, headers=content-length=2, content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:18 GMT, item_count=0 | http_status=200, content_type=application/x-zip-compressed, byte_length=98287, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:19 GMT, manifest=files/open_water_swim-1.manifest.json |
| open_water_swim-2 | open_water_swim | open_water_swimming | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2702, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:21 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2, etag_present=no, last_modified_present=no, headers=content-length=2, content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:22 GMT, item_count=0 | http_status=200, content_type=application/x-zip-compressed, byte_length=92272, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:23 GMT, manifest=files/open_water_swim-2.manifest.json |
| running-1 | running | running | http_status=200, content_type=application/json;charset=UTF-8, byte_length=6590, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:25 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=1153, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:26 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=103283, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:27 GMT, manifest=files/running-1.manifest.json |
| running-2 | running | running | http_status=200, content_type=application/json;charset=UTF-8, byte_length=7151, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:29 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=1152, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:30 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=114250, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:32 GMT, manifest=files/running-2.manifest.json |
| cardio-1 | cardio | indoor_cardio | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2752, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:34 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=4944, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:35 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=16862, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:36 GMT, manifest=files/cardio-1.manifest.json |
| cardio-2 | cardio | indoor_cardio | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2770, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:37 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=4944, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:38 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=15951, etag_present=no, last_modified_present=no, headers=content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:40 GMT, manifest=files/cardio-2.manifest.json |
| other-1 | other | mobility | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2734, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:41 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=3224, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:42 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=7582, etag_present=no, last_modified_present=no, headers=content-length=7582, content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:44 GMT, manifest=files/other-1.manifest.json |
| other-2 | other | mobility | http_status=200, content_type=application/json;charset=UTF-8, byte_length=2736, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:45 GMT | http_status=200, content_type=application/json;charset=UTF-8, byte_length=3224, etag_present=no, last_modified_present=no, headers=content-type=application/json;charset=UTF-8, date=Thu, 22 Oct 2026 13:13:46 GMT, item_count=1 | http_status=200, content_type=application/x-zip-compressed, byte_length=7673, etag_present=no, last_modified_present=no, headers=content-length=7673, content-type=application/x-zip-compressed, date=Thu, 22 Oct 2026 13:13:47 GMT, manifest=files/other-2.manifest.json |

## Categories

| category | status | selected | note | type_key |
| --- | --- | --- | --- | --- |
| explicit | not_observed | 0 | not present in account | - |
| multisport | not_observed | 0 | not present in account | - |
| manual | not_observed | 0 | not present in account | - |
| pool_swim | confirmed | 2 | - | - |
| open_water_swim | confirmed | 2 | - | - |
| running | confirmed | 2 | - | - |
| cardio | confirmed | 2 | - | - |
| other | confirmed | 2 | - | mobility |

## HTTP findings

| Field | Value |
| --- | --- |
| status | not_observed |
| rate_limit_headers_seen | - |
| retry_after_seen | no |
| status_429_count | 0 |
| redirects | - |
| other_header_names | cache-control, cf-cache-status, cf-ray, connection, content-disposition, content-encoding, pragma, referrer-policy, server, transfer-encoding, x-content-type-options, x-frame-options, x-xss-protection |
| failures | - |
| note | absence of rate-limit headers does not prove there are no limits |

## Leak check

| Field | Value |
| --- | --- |
| status | passed |
| failed_fixtures | - |
