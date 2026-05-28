# Trading 212 Public API

Welcome to the official API reference for the Trading 212 Public API! This
guide provides all the information you need to start building your own
trading applications and integrations.


---

# General Information

This API is currently in **beta** and is under active development. We're
continuously adding new features and improvements, and we welcome your
feedback.


### Only for Invest and Stocks ISA

The API described here is enabled and usable only for **Invest and Stocks ISA** account types.



### API Environments

We provide two distinct environments for development and trading:

* **Paper Trading (Demo):** `https://demo.trading212.com/api/v0`

* **Live Trading (Real Money):** `https://live.trading212.com/api/v0`

You can test your applications extensively in the paper trading environment
without risking real funds before moving to live trading.

### ⚠️ API Limitations

Please be aware of the following limitations for any order placement:

* **Supported account types:**  The Trading 212 Public API is enabled and
  usable only for **Invest and Stocks ISA** account types.

* **Order execution:** Orders can be executed only in the **primary account
  currency**

* **Multi-currency:** Multi-currency accounts are not currently supported
  through the API. Meaning your account, position and result values in the
  responses will be in the primary account currency.

### Key Concepts

* **Authentication:** Every request to the API must be authenticated using a
  secure key pair. See the **Authentication** section below for details.

* **Rate Limiting:** All API calls are subject to rate limits to ensure fair
  usage and stability. See the **Rate Limiting** section for a full
  explanation.

* **IP Restrictions:** For enhanced security, you can optionally restrict
  your API keys to a specific set of IP addresses from within your Trading 212
  account settings.

* **Selling Orders:** To execute a sell order, you must provide a
  **negative** value for the `quantity` parameter (e.g., `-10.5`). This is a
  core convention of the API.

---

## Quickstart

This simple example shows you how to retrieve your account summary.

First, you must generate your API keys from within the Trading 212 app. For
detailed instructions, please visit our Help Centre:

* [**How to get your Trading 212 API
  key**](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key)

Once you have your **API Key** and **API Secret**, you can make your first
call using `cURL`:

```bash

# Step 1: Replace with your actual credentials and Base64-encode them.

# The `-n` is important as it prevents adding a newline character.

CREDENTIALS=$(echo -n "<YOUR_API_KEY>:<YOUR_API_SECRET>" | base64)


# Step 2: Make the API call to the live environment using the encoded
credentials.

curl -X GET "https://live.trading212.com/api/v0/equity/account/summary" \
 -H "Authorization: Basic $CREDENTIALS"
```

---

# Authentication

The API uses a secure key pair for authentication on every request. You must
provide your **API Key** as the username and your **API Secret** as the
password, formatted as an HTTP Basic Authentication header.

The `Authorization` header is constructed by Base64-encoding your
`API_KEY:API_SECRET` string and prepending it with `Basic `.

### Building the Authorization Header

Here are examples of how to generate the required value in different
environments.

**Linux or macOS (Terminal)**

You can use the `echo` and `base64` commands. Remember to use the `-n` flag
with `echo` to prevent it from adding a trailing newline, which would
invalidate the credential string.

```bash

# This command outputs the required Base64-encoded string for your header.

echo -n "<YOUR_API_KEY>:<YOUR_API_SECRET>" | base64

```

**Python**

This simple snippet shows how to generate the full header value.

```python

import base64


# 1. Your credentials

api_key = "<YOUR_API_KEY>"

api_secret = "<YOUR_API_SECRET>"


# 2. Combine them into a single string

credentials_string = f"{api_key}:{api_secret}"


# 3. Encode the string to bytes, then Base64 encode it

encoded_credentials =
base64.b64encode(credentials_string.encode('utf-8')).decode('utf-8')


# 4. The final header value

auth_header = f"Basic {encoded_credentials}"


print(auth_header)

```

---

# Rate Limiting

To ensure high performance and fair access for all users, all API endpoints
are subject to rate limiting.


> **IMPORTANT NOTE:** All rate limits are applied on a per-account basis,
> regardless of which API key is used or which IP address the request
> originates from.


Specific rate limits are detailed in the reference for each endpoint.

### Response Headers

Every API response includes the following headers to help you manage your
request frequency and avoid hitting limits.

* `x-ratelimit-limit`: The total number of requests allowed in the current
  time period.

* `x-ratelimit-period`: The duration of the time period in seconds.

* `x-ratelimit-remaining`: The number of requests you have left in the
  current period.

* `x-ratelimit-reset`: A Unix timestamp indicating the exact time when the
  limit will be fully reset.

* `x-ratelimit-used`: The number of requests you have already made in the
  current period.

### How It Works

The rate limiter allows for requests to be made in bursts. For example, an
endpoint with a limit of `50 requests per 1 minute` does **not** strictly
mean you can only make one request every 1.2 seconds. Instead, you could:

* Make a burst of all 50 requests in the first 5 seconds of a minute. You
  would then need to wait for the reset time indicated by the
  `x-ratelimit-reset` header before making more requests.

* Pace your requests evenly, for example, by making one call every 1.2
  seconds, ensuring you always stay within the limit.

### Function-Specific Limits

In addition to the general rate limits on HTTP calls, some actions have
their own functional limits. For example, there is a maximum of **50 pending
orders** allowed per ticker, per account.

# Pagination

All list endpoints in the API that return a collection of items (such as historical orders, dividends, and transactions) use **cursor-based pagination** to handle large data sets.

### Parameters

* **`limit`** (integer): Specifies the maximum number of items to return in a single request.
  * **Default:** 20
  * **Maximum:** 50
* **`cursor`** (string | number): A pointer to a specific item in the dataset. This tells the API where to start the next page of results.

### How to Paginate

The easiest way to paginate is by using the `nextPagePath` field returned in the response.

1.  Make your initial request to a list endpoint (e.g., `/api/v0/equity/history/orders`) with an optional `limit` parameter. Do not include a `cursor`.
2.  The API will return a response object. This object will contain a list of `items` and a `nextPagePath` field.
3.  If the `nextPagePath` field is `null`, you have reached the end of the data, and there are no more pages.
4.  If `nextPagePath` is not `null`, **use the entire string value of `nextPagePath`** as the path for your next request. This string contains all the necessary parameters (like `limit` and `cursor`) to get the next page.
5.  Repeat this process until `nextPagePath` is `null`.

### Example

Here is a step-by-step example of fetching all transactions, 2 at a time.

**Request 1: Get the first page**
```bash
curl -X GET "https://demo.trading212.com/api/v0/equity/history/orders?limit=2" \
     -u "API_KEY:API_SECRET"
```
**Response 1: Note the nextPagePath**
```json
{
  "items": [
    { "id": 987654321, "ticker": "AAPL_US_EQ", ... },
    { "id": 987654320, "ticker": "MSFT_US_EQ", ... }
  ],
  "nextPagePath": "/api/v0/equity/history/orders?limit=2&cursor=1760346100000"
}
```
**Request 2: Use the full nextPagePath for the next request**
```bash
curl -X GET "https://demo.trading212.com/api/v0/equity/history/orders?limit=2&cursor=1760346100000" \
     -u "API_KEY:API_SECRET"
```
**Response 2: Get the next page (and a new nextPagePath)**
```json
{
  "items": [
    { "id": 987654319, "ticker": "AAPL_US_EQ", ... },
    { "id": 987654320, "987654318": "MSFT_US_EQ", ... }
  ],
  "nextPagePath": "/api/v0/equity/history/orders?limit=2&cursor=1660015723000"
}
```
**Request 3: Get the final page**
```bash
curl -X GET "https://demo.trading212.com/api/v0/equity/history/orders?limit=2&cursor=1660015723000" \
     -u "API_KEY:API_SECRET"
```
Response 3: nextPagePath is null, indicating the end
```json
{
  "items": [
    { "id": 987654317, "ticker": "AMZN_US_EQ", ... }
  ],
  "nextPagePath": null
}
```

---

# Useful Links

Here are some additional resources that you may find helpful.

* [**Trading 212 API
  Terms**](https://www.trading212.com/legal-documentation/API-Terms_EN.pdf)

* [**Trading 212 Community Forum**](https://community.trading212.com/) - A
  great place to ask questions and share what you've built.


Version: v0

## Servers

```
https://demo.trading212.com
```

```
https://live.trading212.com
```

## Security

### authWithSecretKey

Use your API Key as the username and your API Secret as the password

Type: http
Scheme: basic

### legacyApiKeyHeader

Type: apiKey
In: header
Name: Authorization

## Download OpenAPI description

[Trading 212 Public API](https://docs.trading212.com/_bundle/api.yaml)

## Accounts

Access fundamental information about your trading account. Retrieve details such as your account ID, currency, and current cash balance.

### Get account summary

 - [GET /api/v0/equity/account/summary](https://docs.trading212.com/api/accounts/getaccountsummary.md): Provides a breakdown of your account's cash and investment metrics,
including available funds, invested capital, and total account value.

Rate limit: 1 req / 5s

## Instruments

Discover what you can trade. These endpoints provide comprehensive lists
of all tradable instruments and the exchanges they belong to, including
details like tickers and trading hours.

### Get exchanges metadata

 - [GET /api/v0/equity/metadata/exchanges](https://docs.trading212.com/api/instruments/exchanges.md): Retrieves all accessible exchanges and their corresponding working schedules.
Data is refreshed every 10 minutes.

Rate limit: 1 req / 30s

### Get all available instruments

 - [GET /api/v0/equity/metadata/instruments](https://docs.trading212.com/api/instruments/instruments.md): Retrieves all accessible instruments.
Data is refreshed every 10 minutes.

Rate limit: 1 req / 50s

## Orders

**⚠️ Order Limitations**

* Orders can be executed only in the **main account currency**


Place, monitor, and cancel equity trade orders. This section provides the
core functionality for programmatically executing your trading strategies
for stocks and ETFs.

### Get all pending orders

 - [GET /api/v0/equity/orders](https://docs.trading212.com/api/orders/orders.md): Retrieves a list of all orders that are currently active (i.e., not yet
filled, cancelled, or expired). This is useful for monitoring the status
of your open positions and managing your trading strategy.

Rate limit: 1 req / 5s

### Place a Limit order

 - [POST /api/v0/equity/orders/limit](https://docs.trading212.com/api/orders/placelimitorder.md): Creates a new Limit order, which executes at a specified price or
better.

- To place a buy order, use a positive quantity. The order will
fill at the limitPrice or lower.

- To place a sell order, use a negative quantity. The order will
fill at the limitPrice or higher.



Order Limitations

* Orders can be executed only in the main account currency


Important: In this beta version, this endpoint is not
idempotent. Sending the same request multiple times may result in
duplicate orders.

Rate limit: 1 req / 2s

### Place a Market order

 - [POST /api/v0/equity/orders/market](https://docs.trading212.com/api/orders/placemarketorder.md): Creates a new Market order, which is an instruction to trade a security
immediately at the next available price. 

- To place a buy order, use a positive quantity. 

- To place a sell order, use a negative quantity.


- extendedHours: Set to true to allow the order to be filled
outside of the standard trading session.

- If placed when the market is closed, the order will be queued to
execute when the market next opens.

x
Order Limitations

* Orders can be executed only in the main account currency


Warning: Market orders can be subject to price slippage, where the
final execution price may differ from the price at the time of order
placement.


Important: In this beta version, this endpoint is not
idempotent. Sending the same request multiple times may result in
duplicate orders.

Rate limit: 50 req / 1m0s

### Place a Stop order

 - [POST /api/v0/equity/orders/stop](https://docs.trading212.com/api/orders/placestoporder_1.md): Creates a new Stop order, which places a Market order once the
stopPrice is reached.

- To place a buy stop order, use a positive quantity.

- To place a sell stop order (commonly a 'stop-loss'), use a
negative quantity.


- The stopPrice is triggered by the instrument's Last Traded Price
(LTP).




Order Limitations

* Orders can be executed only in the main account currency

Important: In this beta version, this endpoint is not
idempotent. Sending the same request multiple times may result in
duplicate orders.

Rate limit: 1 req / 2s

### Place a StopLimit order

 - [POST /api/v0/equity/orders/stop_limit](https://docs.trading212.com/api/orders/placestoporder.md): Creates a new Stop-Limit order, combining features of a Stop and a Limit
order. The direction of the trade (buy/sell) is determined by the sign
of the quantity field.


Execution Logic:

1.  When the instrument's Last Traded Price (LTP) reaches the
specified stopPrice, the order is triggered.

2.  A Limit order is then automatically placed at the specified
limitPrice.


This two-step process helps protect against price slippage that can
occur with a standard Stop order.


Order Limitations

* Orders can be executed only in the main account currency


Important: In this beta version, this endpoint is not
idempotent. Sending the same request multiple times may result in
duplicate orders.

Rate limit: 1 req / 2s

### Cancel a pending order

 - [DELETE /api/v0/equity/orders/{id}](https://docs.trading212.com/api/orders/cancelorder.md): Attempts to cancel an active, unfilled order by its unique ID.
Cancellation is not guaranteed if the order is already in the process of
being filled. A successful response indicates the cancellation request
was accepted.

Rate limit: 50 req / 1m0s

### Get a pending order by ID

 - [GET /api/v0/equity/orders/{id}](https://docs.trading212.com/api/orders/orderbyid.md): Retrieves a single pending order using its unique numerical ID. This is
useful for checking the status of a specific order you have previously
placed.

Rate limit: 1 req / 1s

## Positions

Get a real-time overview of all your open positions, including quantity, average price, and current profit or loss.

### Fetch all open positions

 - [GET /api/v0/equity/positions](https://docs.trading212.com/api/positions/getpositions.md): Fetch all open positions for your account

Rate limit: 1 req / 1s

## Historical events

Review your account's trading history. Access detailed records of past
orders, dividend payments, and cash transactions, or generate downloadable
CSV reports for analysis and record-keeping.

### Get paid out dividends

 - [GET /api/v0/equity/history/dividends](https://docs.trading212.com/api/historical-events/dividends.md): Rate limit: 6 req / 1m0s

### List generated reports

 - [GET /api/v0/equity/history/exports](https://docs.trading212.com/api/historical-events/getreports.md): Retrieves a list of all requested CSV reports and their current status. 


Asynchronous Workflow:

1. Call POST /history/exports to request a report. You will receive a
reportId.

2. Periodically call this endpoint (GET /history/exports) to check the
status of the report corresponding to your reportId.

3. Once the status is Finished, the downloadLink field will contain
a URL to download the CSV file.

Rate limit: 1 req / 1m0s

### Request a CSV report

 - [POST /api/v0/equity/history/exports](https://docs.trading212.com/api/historical-events/requestreport.md): Initiates the generation of a CSV report containing historical account
data. This is an asynchronous operation. The response will include a
reportId which you can use to track the status of the generation
process using the GET /history/exports endpoint.

Rate limit: 1 req / 30s

### Get historical orders data

 - [GET /api/v0/equity/history/orders](https://docs.trading212.com/api/historical-events/orders_1.md): Rate limit: 6 req / 1m0s

### Get transactions

 - [GET /api/v0/equity/history/transactions](https://docs.trading212.com/api/historical-events/transactions.md): Fetch superficial information about movements to and from your account

Rate limit: 6 req / 1m0s

## Pies (Deprecated)

Manage your investment Pies. Use these endpoints to create, view, update, and delete your custom portfolios, making automated and diversified investing simple.

**Deprecation notice:** The current state of the Pies API, while still operational, won't be further supported and updated.

### Fetch all pies (deprecated)

 - [GET /api/v0/equity/pies](https://docs.trading212.com/api/pies-(deprecated)/getall.md): Fetches all pies for the account

Rate limit: 1 req / 30s

### Create pie (deprecated)

 - [POST /api/v0/equity/pies](https://docs.trading212.com/api/pies-(deprecated)/create.md): Creates a pie for the account by given params

Rate limit: 1 req / 5s

### Delete pie (deprecated)

 - [DELETE /api/v0/equity/pies/{id}](https://docs.trading212.com/api/pies-(deprecated)/delete.md): Deletes a pie by given id

Rate limit: 1 req / 5s

### Fetch a pie (deprecated)

 - [GET /api/v0/equity/pies/{id}](https://docs.trading212.com/api/pies-(deprecated)/getdetailed.md): Fetches a pies for the account with detailed information

Rate limit: 1 req / 5s

### Update pie (deprecated)

 - [POST /api/v0/equity/pies/{id}](https://docs.trading212.com/api/pies-(deprecated)/update.md): Updates a pie for the account by given params

Rate limit: 1 req / 5s

### Duplicate pie (deprecated)

 - [POST /api/v0/equity/pies/{id}/duplicate](https://docs.trading212.com/api/pies-(deprecated)/duplicatepie.md): Duplicates a pie for the account 

Rate limit: 1 req / 5s
