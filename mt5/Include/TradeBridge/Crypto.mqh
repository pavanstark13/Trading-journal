//+------------------------------------------------------------------+
//|  Crypto.mqh -- SHA-256, HMAC-SHA256 and UUIDv5 for TradeBridge    |
//|                                                                  |
//|  MQL5's CryptEncode(CRYPT_HASH_SHA256, ...) ignores the key       |
//|  argument, so HMAC is constructed by hand from the RFC 2104       |
//|  ipad/opad definition. The output must match Python's             |
//|  hmac.new(secret, msg, sha256).hexdigest() exactly.               |
//+------------------------------------------------------------------+
#property strict

//--- RFC 4122 namespace for DNS, used as the root of our namespace UUID
const uchar UUID_NS_DNS[16] = {
   0x6b,0xa7,0xb8,0x10,0x9d,0xad,0x11,0xd1,
   0x80,0xb4,0x00,0xc0,0x4f,0xd4,0x30,0xc8
};

//+------------------------------------------------------------------+
void TB_Sha256(const uchar &data[], uchar &out[])
{
   uchar key[];
   ArrayResize(key, 0);
   CryptEncode(CRYPT_HASH_SHA256, data, key, out);
}

//+------------------------------------------------------------------+
void TB_Sha1(const uchar &data[], uchar &out[])
{
   uchar key[];
   ArrayResize(key, 0);
   CryptEncode(CRYPT_HASH_SHA1, data, key, out);
}

//+------------------------------------------------------------------+
//| Convert a string to its UTF-8 bytes WITHOUT a trailing null.      |
//| StringToCharArray appends a terminator; signing it would break    |
//| parity with the server, so it is always trimmed here.             |
//+------------------------------------------------------------------+
void TB_StringToBytes(const string text, uchar &out[])
{
   int len = StringToCharArray(text, out, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0 && out[len - 1] == 0)
      ArrayResize(out, len - 1);
   else
      ArrayResize(out, len);
}

//+------------------------------------------------------------------+
string TB_BytesToHex(const uchar &data[])
{
   string hex = "";
   for(int i = 0; i < ArraySize(data); i++)
      hex += StringFormat("%02x", data[i]);
   return hex;
}

//+------------------------------------------------------------------+
//| HMAC-SHA256. Mirrors app/core/crypto.py:compute_ea_signature.     |
//+------------------------------------------------------------------+
void TB_HmacSha256(const uchar &key[], const uchar &message[], uchar &out[])
{
   const int BLOCK = 64;
   uchar padded[];
   ArrayResize(padded, BLOCK);
   ArrayInitialize(padded, 0);

   if(ArraySize(key) > BLOCK)
   {
      uchar hashed[];
      TB_Sha256(key, hashed);
      ArrayCopy(padded, hashed, 0, 0, ArraySize(hashed));
   }
   else
   {
      ArrayCopy(padded, key, 0, 0, ArraySize(key));
   }

   uchar inner[], outer[];
   ArrayResize(inner, BLOCK + ArraySize(message));
   ArrayResize(outer, BLOCK + 32);

   for(int i = 0; i < BLOCK; i++)
   {
      inner[i] = (uchar)(padded[i] ^ 0x36);
      outer[i] = (uchar)(padded[i] ^ 0x5C);
   }
   ArrayCopy(inner, message, BLOCK, 0, ArraySize(message));

   uchar innerHash[];
   TB_Sha256(inner, innerHash);
   ArrayCopy(outer, innerHash, BLOCK, 0, 32);

   TB_Sha256(outer, out);
}

//+------------------------------------------------------------------+
string TB_SignRequest(const string secret, const string timestamp,
                      const string nonce, const string body)
{
   uchar keyBytes[], messageBytes[], signature[];
   TB_StringToBytes(secret, keyBytes);
   TB_StringToBytes(timestamp + "." + nonce + "." + body, messageBytes);
   TB_HmacSha256(keyBytes, messageBytes, signature);
   return TB_BytesToHex(signature);
}

//+------------------------------------------------------------------+
//| UUID v5 (SHA-1 based, RFC 4122).                                  |
//| Mirrors Python's uuid.uuid5(namespace, name).                     |
//+------------------------------------------------------------------+
string TB_FormatUuid(const uchar &bytes[])
{
   string hex = TB_BytesToHex(bytes);
   return StringSubstr(hex, 0, 8) + "-" + StringSubstr(hex, 8, 4) + "-" +
          StringSubstr(hex, 12, 4) + "-" + StringSubstr(hex, 16, 4) + "-" +
          StringSubstr(hex, 20, 12);
}

//+------------------------------------------------------------------+
void TB_Uuid5(const uchar &namespaceBytes[], const string name, uchar &out[])
{
   uchar nameBytes[], combined[], digest[];
   TB_StringToBytes(name, nameBytes);

   ArrayResize(combined, 16 + ArraySize(nameBytes));
   ArrayCopy(combined, namespaceBytes, 0, 0, 16);
   ArrayCopy(combined, nameBytes, 16, 0, ArraySize(nameBytes));

   TB_Sha1(combined, digest);

   ArrayResize(out, 16);
   ArrayCopy(out, digest, 0, 0, 16);
   out[6] = (uchar)((out[6] & 0x0F) | 0x50);   // version 5
   out[8] = (uchar)((out[8] & 0x3F) | 0x80);   // RFC 4122 variant
}

//+------------------------------------------------------------------+
//| Resolve the deployment namespace UUID once, at EA start.          |
//+------------------------------------------------------------------+
void TB_NamespaceUuid(const string namespaceName, uchar &out[])
{
   uchar dns[16];
   ArrayCopy(dns, UUID_NS_DNS, 0, 0, 16);
   TB_Uuid5(dns, namespaceName, out);
}

//+------------------------------------------------------------------+
//| 32 hex characters of request nonce. Uniqueness matters more than  |
//| cryptographic quality: the server also enforces a timestamp       |
//| window, so a nonce only needs to be unique within 5 minutes.      |
//+------------------------------------------------------------------+
string TB_Nonce()
{
   string raw = "";
   for(int i = 0; i < 8; i++)
      raw += StringFormat("%04x", MathRand());
   return StringSubstr(raw, 0, 32);
}
