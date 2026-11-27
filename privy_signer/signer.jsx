import React, {useEffect} from 'react';
import {createRoot} from 'react-dom/client';
import {PrivyProvider, useIdentityToken, useLoginWithEmail, usePrivy, useWallets} from '@privy-io/react-auth';
import {hashTypedData, recoverTypedDataAddress} from 'viem';
import {baseSepolia} from 'viem/chains';

const sameAddress = (left, right) => typeof left === 'string' && left.toLowerCase() === right.toLowerCase();

function WalletSigner() {
  const {ready, authenticated, user} = usePrivy();
  const {ready: walletsReady, wallets} = useWallets();
  const {identityToken} = useIdentityToken();
  const {sendCode, loginWithCode} = useLoginWithEmail();

  function signerWallet(address) {
    return wallets.find(wallet =>
      wallet.connectorType === 'embedded' && sameAddress(wallet.address, address)
    );
  }

  useEffect(() => {
    window.previewWalletSigner = {
      ready,
      identityToken: authenticated ? identityToken : null,
      sendCode: email => sendCode({email, disableSignup: true}),
      login: code => loginWithCode({code}),
      hasWallet: (signer, maker) => authenticated && walletsReady &&
        Boolean(signerWallet(signer)) && user?.linkedAccounts.some(account =>
          account.type === 'smart_wallet' && sameAddress(account.address, maker)
        ),
      async sign(signer, typedData, orderHash) {
        const wallet = signerWallet(signer);
        if (!authenticated || !wallet || hashTypedData(typedData).toLowerCase() !== orderHash) {
          throw new Error('Wallet or order hash mismatch');
        }
        const provider = await wallet.getEthereumProvider();
        const signature = await provider.request({
          method: 'eth_signTypedData_v4',
          params: [signer, JSON.stringify(typedData)],
        });
        if (!sameAddress(await recoverTypedDataAddress({...typedData, signature}), signer)) {
          throw new Error('Recovered signer mismatch');
        }
        return signature;
      },
    };
    return () => { delete window.previewWalletSigner; };
  }, [ready, authenticated, user, walletsReady, wallets, identityToken, sendCode, loginWithCode]);

  return null;
}

const {appId, clientId} = window.previewWalletConfig;
createRoot(document.getElementById('app')).render(
  <PrivyProvider
    appId={appId}
    clientId={clientId}
    config={{
      loginMethods: ['email'],
      defaultChain: baseSepolia,
      supportedChains: [baseSepolia],
      embeddedWallets: {ethereum: {createOnLogin: 'off'}, showWalletUIs: false},
    }}
  >
    <WalletSigner />
  </PrivyProvider>,
);
