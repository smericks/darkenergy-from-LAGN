import jax_cosmo
import jax.numpy as jnp
import numpy as np
import pandas as pd
from scipy.stats import norm, truncnorm, uniform, gaussian_kde
from astropy.cosmology import w0waCDM
import tdc_utils
import emcee
import time


"""
cosmo_models available: 
    'LCDM': [H0,OmegaM,mu(gamma_lens),sigma(gamma_lens)]
    'w0waCDM': [H0,OmegaM,w0,wa,mu(gamma_lens),sigma(gamma_lens)]
    'LCDM_lambda_int': [H0,OmegaM,mu(lambda_int),sigma(lambda_int),
        mu(gamma_lens),sigma(gamma_lens)]
"""

###########################
# TDC Likelihood Functions
###########################

# I think we want this to be a class, so we can keep track of quantities
# internally 
class TDCLikelihood():

    def __init__(self,td_measured,td_likelihood_prec,
        fpd_samples,gamma_pred_samples,z_lens,z_src,
        log_prob_modeling_prior=None,cosmo_model='LCDM',
        use_gamma_info=True,use_astropy=False):
        """
        Keep track of quantities that remain constant throughout the inference

        Args: 
            td_measured: array of td_measured
                doubles: (n_lenses,1)
                quads: (n_lenses,3)
            td_likelihood_prec: array of precision matrices: 
                - doubles: ((1/sigma^2))
                - quads: ((1/sigma^2 0 0), (0 1/sigma^2 0), (0 0 1/sigma^2))
            fpd_samples: array of fermat potential difference posterior samples
                doubles: (n_lenses,n_fpd_samples,1)
                quads: (n_lenses,n_fpd_samples,3)
            gamma_pred_samples (np.array(float)): 
                gamma samples associated with each set of fpd samples.
                (n_lenses,n_fpd_samples)
            z_lens (np.array(float), size:(n_lenses)): lens redshifts
            z_src (np.array(float), size:(n_lenses)): source redshifts
            log_prob_modeling_prior: TODO
            cosmo_model (string): 'LCDM' or 'w0waCDM'
            use_gamma_info (bool): If False, removes reweighting from likelihood
                evaluation (any population level gamma params should just 
                return the prior then...)
        """

        # no processing needed (np.squeeze ensures any dimensions of size 1
        #    are removed)
        self.z_lens = np.squeeze(np.asarray(z_lens))
        self.z_src = np.squeeze(np.asarray(z_src))
        if cosmo_model not in ['LCDM','LCDM_lambda_int','w0waCDM']:
            raise ValueError("choose from available cosmo_models: "+
                "LCDM, LCDM_lambda_int, w0waCDM")
        self.cosmo_model = cosmo_model
        self.use_gamma_info = use_gamma_info
        self.use_astropy = use_astropy
        # make sure the dims are right
        self.num_lenses = fpd_samples.shape[0]
        self.num_fpd_samples = fpd_samples.shape[1]
        self.dim_fpd = fpd_samples.shape[2]
        
        # keep track internally
        self.gamma_pred_samples = gamma_pred_samples
        self.fpd_samples = fpd_samples
        # pad with a 2nd batch dim for # of fpd samples
        self.td_measured = np.repeat(td_measured[:, np.newaxis, :],
            self.num_fpd_samples, axis=1)
        self.td_likelihood_prec = np.repeat(td_likelihood_prec[:, np.newaxis, :, :],
            self.num_fpd_samples, axis=1)
        # TODO: compute likelihood prefactors here instead of feeding them in...
        # log( (1/(2pi)^k/2) * 1/sqrt(det(Sigma)) )
        k_dim = self.dim_fpd
        self.td_likelihood_prefactors = np.log( (1/(2*np.pi)**(k_dim/2)) / 
            np.sqrt(np.linalg.det(np.linalg.inv(self.td_likelihood_prec))) )

        # TODO: fix hardcoding of this
        if log_prob_modeling_prior is None:
            self.log_prob_modeling_prior = uniform.logpdf(gamma_pred_samples,loc=1.,scale=2.)
            #self.log_prob_modeling_prior = norm.logpdf(gamma_pred_samples,loc=2.,scale=0.2)
        else:
            hst_train0 = pd.read_csv(r'/Users/smericks/Desktop/StrongLensing/darkenergy-from-LAGN/MassModels/hst_train0_metadata.csv')
            gamma_vals = hst_train0['main_deflector_parameters_gamma'].to_numpy().astype(float)
            gamma_kde = gaussian_kde(gamma_vals)
            self.log_prob_modeling_prior = np.empty((gamma_pred_samples.shape))
            for i in range(0,gamma_pred_samples.shape[0]):
                self.log_prob_modeling_prior[i,:] = gamma_kde.logpdf(gamma_pred_samples[i])

    # compute predicted time delays from predicted fermat potential differences
    # requires an assumed cosmology (from hyperparameters) and redshifts
    def td_pred_from_fpd_pred(self,proposed_cosmo,lambda_int_samples=None):
        """
        Args:
            proposed_cosmo (default: jax_cosmo.Cosmology): built by
                construct_proposed_cosmo() (see below)
            lambda_int_samples (): shape=(num_lenses,num_fpd_samples)

        Returns:
            td_pred_samples (size:(n_lenses,n_samples,3))
        """

        if self.use_astropy:
            Ddt_computed = tdc_utils.ddt_from_redshifts(proposed_cosmo,
                self.z_lens,self.z_src)
        else:
            Ddt_computed = tdc_utils.jax_ddt_from_redshifts(proposed_cosmo,
                self.z_lens,self.z_src)
        
        Ddt_computed = np.array(Ddt_computed)
        # add batch dimensions for Ddt computed...
        Ddt_repeated = np.repeat(Ddt_computed[:, np.newaxis],
            self.num_fpd_samples, axis=1)
        Ddt_repeated = np.repeat(Ddt_repeated[:,:, np.newaxis],
            self.dim_fpd, axis=2)
        # compute predicted time delays (this function should work w/ arrays)
        td_pred = tdc_utils.td_from_ddt_fpd(Ddt_repeated,self.fpd_samples)
        
        # Linear scaling if lambda_int is present
        if lambda_int_samples is not None:
            lambda_int_repeated = np.repeat(lambda_int_samples[:,:,np.newaxis],
                self.dim_fpd, axis=2)
            td_pred *= lambda_int_repeated

        return td_pred

    # TDC Likelihood per lens per fpd sample (only condense along num. images dim.)
    def td_log_likelihood_per_samp(self,td_pred_samples):
        """
        Args:
            td_pred_samples (n_lenses,n_fpd_samps,3)

        Returns:
            td_log_likelihood_per_fpd_samp (n_lenses,n_fpd_samps)
        """

        x_minus_mu = (td_pred_samples-self.td_measured)
        # add dimension s.t. x_minus_mu is 2D
        x_minus_mu = np.expand_dims(x_minus_mu,axis=-1)
        # matmul should condense the (# of time delays) dim.
        exponent = -0.5*np.matmul(np.transpose(x_minus_mu,axes=(0,1,3,2)),
            np.matmul(self.td_likelihood_prec,x_minus_mu))

        # reduce to two dimensions: (n_lenses,n_fpd_samples)
        exponent = np.squeeze(exponent)

        # log-likelihood
        return self.td_likelihood_prefactors + exponent
        
        
    def construct_proposed_cosmo(self,hyperparameters):
        """
        Args:
            hyperparameters (): 
                - LCDM order: [H0,Omega_M,mu_gamma,sigma_gamma]
                - LCDM_lambda_int order: [H0,Omega_M,mu_lambda_int,
                    sigma_lambda_int,mu_gamma,sigma_gamma]
                - w0waCDM order: [H0,Omega_M,w0,wa,mu_gamma,sigma_gamma]
        """
        # construct cosmology object from hyperparameters
        h0_input = hyperparameters[0]
        # NOTE: baryonic fraction hardcoded to 0.05
        omega_m_input = hyperparameters[1]
        omega_c_input = hyperparameters[1] - 0.05 # CDM fraction
        omega_de_input = 1. - omega_m_input
        if self.cosmo_model in ['LCDM','LCDM_lambda_int'] :
             w0_input = -1.
             wa_input = 0.
        elif self.cosmo_model == 'w0waCDM':
             w0_input = hyperparameters[2]
             wa_input = hyperparameters[3]

        if self.use_astropy: 
            # instantiate astropy cosmology object
            astropy_cosmo = w0waCDM(H0=h0_input,
                Om0=omega_m_input,Ode0=omega_de_input,
                w0=w0_input,wa=wa_input)
            
            return astropy_cosmo

        else:
            # NOTE: baryonic fraction hardcoded to 0.05
            my_jax_cosmo = jax_cosmo.Cosmology(h=jnp.float32(h0_input/100),
                        Omega_c=jnp.float32(omega_c_input), # "cold dark matter fraction"
                        Omega_b=jnp.float32(0.05), # "baryonic fraction"
                        Omega_k=jnp.float32(0.),
                        w0=jnp.float32(w0_input),
                        wa=jnp.float32(wa_input),sigma8 = jnp.float32(0.8), n_s=jnp.float32(0.96))
            
            return my_jax_cosmo
        
    def process_hyperparam_proposal(self,hyperparameters):
        """
        Args: 
            hyperparameters (): 
                - LCDM order: [H0,Omega_M,mu_gamma,sigma_gamma]
                - LCDM_lambda_int order: [H0,Omega_M,mu_lambda_int,
                    sigma_lambda_int,mu_gamma,sigma_gamma]
                - w0waCDM order: [H0,Omega_M,w0,wa,mu_gamma,sigma_gamma]
        Returns: 
            proposed_cosmo (default=jax_cosmo.Cosmology)
            lambda_int_samples (): Set to None if no lambda_int in hypermodel.
                If in hypermodel, shape=(num_lenses,num_fpd_samples)
        """

        # importance sampling over lambda_int based on proposal distribution
        lambda_int_samples = None
        if self.cosmo_model == 'LCDM_lambda_int':
            # NOTE: hardcoding of hyperparameter order!! (-4 is mu, -3 is sigma)
            mu_lint = hyperparameters[-4]
            sigma_lint = hyperparameters[-3]
            # truncating to avoid values below 0 (unphysical)
            lambda_int_samples = truncnorm.rvs(-mu_lint/sigma_lint,np.inf,
                loc=mu_lint,scale=sigma_lint,
                size=(self.num_lenses,self.num_fpd_samples))

        return self.construct_proposed_cosmo(hyperparameters),lambda_int_samples

    def full_log_likelihood(self,hyperparameters):
        """
        Args:
            hyperparameters ([H0,mu_gamma,sigma_gamma] or [H0,w0,wa,mu_gamma,sigma_gamma])
            fpd_pred_samples (size:(n_lenses,n_samples,3)): Note, it is assumed 
                that doubles are padded w/ zeros
        """
        
        # construct cosmology + lint samps (if required) from hyperparameters
        proposed_cosmo, lambda_int_samples = self.process_hyperparam_proposal(
            hyperparameters)

        # td_pred_samples from fpd_pred_samples
        td_pred_samples = self.td_pred_from_fpd_pred(proposed_cosmo,
            lambda_int_samples)
        td_log_likelihoods = self.td_log_likelihood_per_samp(td_pred_samples)

        # reweighting factor
        # NOTE: hardcoding of hyperparameter order!! (-2 is mu, -1 is sigma)
        if self.use_gamma_info:
            eval_at_proposed_nu = norm.logpdf(self.gamma_pred_samples,
                loc=hyperparameters[-2],scale=hyperparameters[-1])
            rw_factor = eval_at_proposed_nu - self.log_prob_modeling_prior
        else:
            rw_factor = 0.

        # sum across fpd samples 
        individ_likelihood = np.mean(np.exp(td_log_likelihoods+rw_factor),axis=1)

        # sum over all lenses
        if np.sum(individ_likelihood == 0) > 0:
            return -np.inf

        log_likelihood = np.sum(np.log(individ_likelihood))

        return log_likelihood

############
# TDC + Kin
############

# TODO: implement this class
class TDCKinLikelihood(TDCLikelihood):

    def __init__(self,td_measured,td_likelihood_prec,
        sigma_v_measured,sigma_v_likelihood_prec,
        fpd_samples,gamma_pred_samples,kin_pred_samples,z_lens,z_src,
        log_prob_modeling_prior=None,cosmo_model='LCDM',use_gamma_info=True,
        use_astropy=False):
        """
        Keep track of quantities that remain constant throughout the inference

        Args: 
            td_measured: array of td_measured 
                doubles: (n_lenses,1)
                quads: (n_lenses,3)
            td_likelihood_prec: array of precision matrices: 
                - doubles: ((1/sigma^2))
                - quads: ((1/sigma^2 0 0), (0 1/sigma^2 0), (0 0 1/sigma^2))
            sigma_v_measured: 
                single-aperture: (n_lenses,1)
                IFU: (n_lenses,n_bins)
            sigma_v_likelihood_prec: array of Gaussian measurement error 
                in the form of precision matrices
            fpd_samples: array of fermat potential difference posterior samples
                doubles: (n_lenses,n_fpd_samples,1)
                quads: (n_lenses,n_fpd_samples,3)
            gamma_pred_samples (np.array(float)): 
                gamma samples associated with each set of fpd samples.
                (n_lenses,n_fpd_samples)
            kin_pred_samples (np.array(float)): this tracks the quantity:
                 c*sqrt(mathcal{J}(xi_mass,xi_light,beta_ani)),
                this is the "dimensionless and cosmology-independent term of 
                the Jeans equation", see Eqn 17 in TDCOSMO IV:  
                 sigma_v = c * sqrt(D_s/D_ds) * sqrt(mathcal{J})
                size of array: (n_lenses,n_fpd_samples,num_kin_bins)
            z_lens (np.array(float), size:(n_lenses)): lens redshifts
            z_src (np.array(float), size:(n_lenses)): source redshifts
            log_prob_modeling_prior: TODO
            cosmo_model (string): 'LCDM' or 'w0waCDM'
            use_gamma_info (bool): If False, removes reweighting from likelihood
                evaluation (any population level gamma params should just 
                return the prior then...)
        """

        super().__init__(td_measured,td_likelihood_prec,fpd_samples,
            gamma_pred_samples,z_lens,z_src,
            log_prob_modeling_prior,cosmo_model,use_gamma_info,
            use_astropy)

        # track kinematic information
        self.num_kin_bins = kin_pred_samples.shape[2]
        self.kin_pred_samples = kin_pred_samples

        # pad measurements with a 2nd batch dim for # of fpd samples
        self.sigma_v_measured = np.repeat(sigma_v_measured[:, np.newaxis, :],
            self.num_fpd_samples, axis=1)
        self.sigma_v_likelihood_prec = np.repeat(
            sigma_v_likelihood_prec[:, np.newaxis, :, :],
            self.num_fpd_samples, axis=1)
        
        k_dim = self.num_kin_bins
        self.sigma_v_likelihood_prefactors = np.log( (1/(2*np.pi)**(k_dim/2)) / 
            np.sqrt(np.linalg.det(np.linalg.inv(self.sigma_v_likelihood_prec))))
        

    
    def sigma_v_pred_from_kin_pred(self,proposed_cosmo,lambda_int_samples=None):
        """
        Args:
            proposed_cosmo (default: jax_cosmo.Cosmology): built by
                construct_proposed_cosmo() (see below)
            lambda_int_samples (): shape=(num_lenses,num_fpd_samples)
        """

        if self.use_astropy:
            raise ValueError("astropy option not implemented for TDC+Kin")
        else:
            Ds_div_Dds_computed = tdc_utils.jax_kin_distance_ratio(
                proposed_cosmo,self.z_lens,self.z_src)
        
        Ds_div_Dds_computed = np.array(Ds_div_Dds_computed)
        # add batch dimensions for fpd_samples
        Ds_div_Dds_repeated = np.repeat(Ds_div_Dds_computed[:, np.newaxis],
            self.num_fpd_samples, axis=1) 
        # add batch dimension for # kinematic bins
        Ds_div_Dds_repeated = np.repeat(Ds_div_Dds_repeated[:, :, np.newaxis],
            self.num_kin_bins, axis=2) 
        # scale the kin_pred with cosmology term: sigma_v = sqrt(Ds/Dds)*c*sqrt(mathcal{J})
        sigma_v_pred = np.sqrt(Ds_div_Dds_repeated)*self.kin_pred_samples

        # sqrt(lambda) scaling if lambda_int is present
        if lambda_int_samples is not None:
            lambda_int_repeated = np.repeat(lambda_int_samples[:,:,np.newaxis],
                self.num_kin_bins, axis=2)
            sigma_v_pred *= np.sqrt(lambda_int_repeated)

        return sigma_v_pred
    

    def sigma_v_log_likelihood_per_samp(self,sigma_v_pred_samples):
        """
        Args:
            sigma_v_pred_samples (n_lenses,n_fpd_samps,num_kin_bins)

        Returns:
            td_log_likelihood_per_fpd_samp (n_lenses,n_fpd_samps)
        """

        x_minus_mu = (sigma_v_pred_samples-self.sigma_v_measured)
        # add dimension s.t. x_minus_mu is 2D
        x_minus_mu = np.expand_dims(x_minus_mu,axis=-1)
        # matmul should condense the (# of time delays) dim.
        exponent = -0.5*np.matmul(np.transpose(x_minus_mu,axes=(0,1,3,2)),
            np.matmul(self.sigma_v_likelihood_prec,x_minus_mu))

        # reduce to two dimensions: (n_lenses,n_fpd_samples)
        exponent = np.squeeze(exponent)

        # log-likelihood
        return self.sigma_v_likelihood_prefactors + exponent


    def full_log_likelihood(self, hyperparameters):

        # construct cosmology from hyperparameters
        proposed_cosmo, lambda_int_samples = self.process_hyperparam_proposal(
            hyperparameters)

        # td log likelihood per sample
        td_pred_samples = self.td_pred_from_fpd_pred(
            proposed_cosmo,lambda_int_samples)
        td_log_likelihoods = self.td_log_likelihood_per_samp(td_pred_samples)

        # kin log likelihood per sample
        sigma_v_pred_samples = self.sigma_v_pred_from_kin_pred(
            proposed_cosmo,lambda_int_samples)
        sigma_v_log_likelihoods = self.sigma_v_log_likelihood_per_samp(sigma_v_pred_samples)

        # reweighting factor
        # NOTE: hardcoding of hyperparameter order!! (-2 is mu, -1 is sigma)
        if self.use_gamma_info:
            eval_at_proposed_nu = norm.logpdf(self.gamma_pred_samples,
                loc=hyperparameters[-2],scale=hyperparameters[-1])
            rw_factor = eval_at_proposed_nu - self.log_prob_modeling_prior
        else:
            rw_factor = 0.

        # sum across fpd samples 
        individ_likelihood = np.mean(
            np.exp(td_log_likelihoods+sigma_v_log_likelihoods+rw_factor),
            axis=1)

        # sum over all lenses
        if np.sum(individ_likelihood == 0) > 0:
            return -np.inf

        log_likelihood = np.sum(np.log(individ_likelihood))

        return log_likelihood


#########################
# Sampling Implementation
#########################

def fast_TDC(tdc_likelihood_list,num_emcee_samps=1000,
    n_walkers=20):
    """
    Args:
        tdc_likelihood_list ([TDCLikelihood]): list of likelihood objects 
            (will add log likelihoods together)
        num_emcee_samps (int): Number of iterations for MCMC inference
        n_walkers (int): Number of emcee walkers
        
    Returns: 
        mcmc chain (emcee.EnsemblerSampler.chain)
    """

    # Retrieve cosmo_model from likelihood object?
    cosmo_model = tdc_likelihood_list[0].cosmo_model
    for i in range(1,len(tdc_likelihood_list)):
        if tdc_likelihood_list[i].cosmo_model != cosmo_model:
            raise ValueError("")

    def LCDM_log_prior(hyperparameters):
        """
        Args:
            hyperparameters ([H0,omega_M,mu_gamma,sigma_gamma])
        """

        if hyperparameters[0] < 0 or hyperparameters[0] > 150: #h0
            return -np.inf
        if hyperparameters[1] < 0.05 or hyperparameters[1] > 0.5: #omega_M 
            return -np.inf
        elif hyperparameters[2] < 1.5 or hyperparameters[2] > 2.5: #mu(gamma_lens)
            return -np.inf
        elif hyperparameters[3] < 0.001 or hyperparameters[3] > 0.2: #sigma(gamma_lens)
            return -np.inf
        
        return 0
    
    def LCDM_lambda_int_log_prior(hyperparameters):
        """
        Args:
            hyperparameters ([H0,omega_M,mu_lambda_int,sigma_lambda_int,
                mu_gamma,sigma_gamma])
        """

        if hyperparameters[0] < 0 or hyperparameters[0] > 150: #h0
            return -np.inf
        if hyperparameters[1] < 0.05 or hyperparameters[1] > 0.5: #omega_M 
            return -np.inf
        elif hyperparameters[2] < 0.5 or hyperparameters[2] > 1.5: #mu(lambda_int)
            return -np.inf
        elif hyperparameters[3] < 0.001 or hyperparameters[3] > 0.5: #sigma(lambda_int)
            return -np.inf
        elif hyperparameters[4] < 1.5 or hyperparameters[4] > 2.5: #mu(gamma_lens)
            return -np.inf
        elif hyperparameters[5] < 0.001 or hyperparameters[5] > 0.2: #sigma(gamma_lens)
            return -np.inf
        
        return 0
    
    def w0waCDM_log_prior(hyperparameters):
        """
        Args:
            hyperparameters ([H0,Omega_M,w0,wa,mu_gamma,sigma_gamma])
        """

        # h0 [0,150]
        if hyperparameters[0] < 0 or hyperparameters[0] > 150: 
            return -np.inf
        # Omega_M [0.05,0.5]
        if hyperparameters[1] < 0.05 or hyperparameters[1] > 0.5: 
            return -np.inf
        #w0 [-2,0]
        elif hyperparameters[2] < -2 or hyperparameters[2] > 0:
            return -np.inf
        #wa [-2,2]
        elif hyperparameters[3] < -2 or hyperparameters[3] > 2:
            return -np.inf
        #mu(gamma)
        elif hyperparameters[4] < 1.5 or hyperparameters[4] > 2.5:
            return -np.inf
        #sigma(gamma)
        elif hyperparameters[5] < 0.001 or hyperparameters[5] > 0.2:
            return -np.inf
        
        return 0
    
    def generate_initial_state(n_walkers,cosmo_model):
        """
        Args:
            n_walkers (int): number of emcee walkers
            cosmo_model (string): 'LCDM' or 'w0waCDM'
        """

        if cosmo_model == 'LCDM':
            # order: [H0,Omega_M,mu_gamma,sigma_gamma]
            cur_state = np.empty((n_walkers,4))
            cur_state[:,0] = uniform.rvs(loc=40,scale=60,size=n_walkers) #h0
            cur_state[:,1] = uniform.rvs(loc=0.1,scale=0.35,size=n_walkers) #Omega_M
            cur_state[:,2] = uniform.rvs(loc=1.5,scale=1.,size=n_walkers)
            cur_state[:,3] = uniform.rvs(loc=0.001,scale=0.199,size=n_walkers)

            return cur_state
        
        if cosmo_model == 'LCDM_lambda_int':
            # order: [H0,Omega_M,mu_lambda_int,sigma_lambda_int,mu_gamma,sigma_gamma]
            cur_state = np.empty((n_walkers,6))
            cur_state[:,0] = uniform.rvs(loc=40,scale=60,size=n_walkers) #h0
            cur_state[:,1] = uniform.rvs(loc=0.1,scale=0.35,size=n_walkers) #Omega_M
            cur_state[:,2] = uniform.rvs(loc=0.9,scale=0.2,size=n_walkers)
            cur_state[:,3] = uniform.rvs(loc=0.001,scale=0.499,size=n_walkers)
            cur_state[:,4] = uniform.rvs(loc=1.5,scale=1.,size=n_walkers)
            cur_state[:,5] = uniform.rvs(loc=0.001,scale=0.199,size=n_walkers)

            return cur_state
        
        elif cosmo_model == 'w0waCDM':
            # order: [H0,Omega_M,w0,wa,mu_gamma,sigma_gamma]
            cur_state = np.empty((n_walkers,6))
            cur_state[:,0] = uniform.rvs(loc=40,scale=60,size=n_walkers) #h0
            cur_state[:,1] = uniform.rvs(loc=0.1,scale=0.35,size=n_walkers) #Omega_M
            cur_state[:,2] = uniform.rvs(loc=-1.5,scale=1.,size=n_walkers)
            cur_state[:,3] = uniform.rvs(loc=-1,scale=2,size=n_walkers)
            cur_state[:,4] = uniform.rvs(loc=1.5,scale=1.,size=n_walkers)
            cur_state[:,5] = uniform.rvs(loc=0.001,scale=0.19,size=n_walkers)

            return cur_state


    
    def log_posterior(hyperparameters):
        """
        Args:
            hyperparameters ([float]): 
                - LCDM: [H0,Omega_M,mu_gamma,sigma_gamma] 
                - w0waCDM: [H0,Omega_M,w0,wa,mu_gamma,sigma_gamma]
        """
        # Prior
        if cosmo_model == 'LCDM':
            lp = LCDM_log_prior(hyperparameters)
        elif cosmo_model == 'LCDM_lambda_int':
            lp = LCDM_lambda_int_log_prior(hyperparameters)
        elif cosmo_model == 'w0waCDM':
            lp = w0waCDM_log_prior(hyperparameters)
        # Likelihood
        if lp == 0:
            for tdc_likelihood in tdc_likelihood_list:
                fll = tdc_likelihood.full_log_likelihood(hyperparameters)
                lp += fll

        return lp
    
    # emcee stuff here
    cur_state = generate_initial_state(n_walkers,cosmo_model)
    sampler = emcee.EnsembleSampler(n_walkers,cur_state.shape[1],log_posterior)

    # run mcmc
    tik_mcmc = time.time()
    _ = sampler.run_mcmc(cur_state,nsteps=num_emcee_samps,progress=True)
    tok_mcmc = time.time()
    print("Avg. Time per MCMC Step: %.3f seconds"%((tok_mcmc-tik_mcmc)/num_emcee_samps))

    return sampler.get_chain()