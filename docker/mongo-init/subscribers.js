// =============================================================================
// subscribers.js — Idempotent provisioning of test subscribers in Open5GS.
//
// Executed by the `subscriber-init` one-shot container (mongosh).
// Creates IMSI 999700000000001 .. 9997000000000NN (NN = $UE_COUNT, default 20)
// using the default UERANSIM credentials (K / OPc), slice SST=1, DNN "internet".
// Re-running it is safe: documents are upserted by IMSI.
// =============================================================================

const UE_COUNT = parseInt(process.env.UE_COUNT || "20", 10);
const K   = "465B5CE8B199B49FAA5F0A2EE238A6BC";
const OPC = "E8ED289DEBA952E4283B54E88E6183CA";
const PLMN = "99970";                                     // MCC 999 / MNC 70

const ambr = { downlink: { value: 1, unit: 3 }, uplink: { value: 1, unit: 3 } }; // 1 Gbps

for (let i = 1; i <= UE_COUNT; i++) {
  const imsi = PLMN + String(i).padStart(10, "0");
  db.subscribers.updateOne(
    { imsi: imsi },
    {
      $set: {
        schema_version: 1,
        imsi: imsi,
        msisdn: [], imeisv: [], mme_host: [], mme_realm: [], purge_flag: [],
        security: { k: K, op: null, opc: OPC, amf: "8000", sqn: NumberLong("1") },
        ambr: ambr,
        slice: [{
          sst: 1,
          default_indicator: true,
          session: [{
            name: "internet",
            type: 3,                                      // IPv4v6
            qos: { index: 9, arp: { priority_level: 8, pre_emption_capability: 1, pre_emption_vulnerability: 1 } },
            ambr: ambr,
            pcc_rule: [],
          }],
        }],
        access_restriction_data: 32,
        subscriber_status: 0,
        operator_determined_barring: 0,
        network_access_mode: 0,
        subscribed_rau_tau_timer: 12,
        __v: 0,
      },
    },
    { upsert: true }
  );
}

print(`[subscriber-init] ${UE_COUNT} subscribers provisioned — total in DB: ${db.subscribers.countDocuments()}`);
